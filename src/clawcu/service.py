from __future__ import annotations

import copy
import json
import os
import re
import socket
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Callable

from clawcu import __version__ as clawcu_version
from clawcu.docker import DockerManager
from clawcu.models import InstanceRecord, InstanceSpec
from clawcu.openclaw import (
    DEFAULT_OPENCLAW_IMAGE_REPO,
    DEFAULT_OPENCLAW_IMAGE_REPO_CN,
    OpenClawManager,
)
from clawcu.storage import StateStore
from clawcu.subprocess_utils import CommandError, run_command
from clawcu.validation import (
    build_instance_record,
    container_name_for_instance,
    image_tag_for_version,
    normalize_version,
    resolve_datadir,
    updated_record,
    upstream_ref_for_version,
    utc_now_iso,
    validate_cpu,
    validate_memory,
    validate_name,
    validate_port,
)


class ClawCUService:
    DEFAULT_OPENCLAW_PORT = 18789
    PORT_SEARCH_STEP = 10
    PORT_SEARCH_LIMIT = 100
    STARTUP_POLL_INTERVAL_SECONDS = 10.0
    STARTUP_PROGRESS_INTERVAL_SECONDS = 10.0
    Reporter = Callable[[str], None]

    def __init__(
        self,
        store: StateStore | None = None,
        docker: DockerManager | None = None,
        openclaw: OpenClawManager | None = None,
        reporter: Reporter | None = None,
        runner: Callable | None = None,
    ):
        self.store = store or StateStore()
        self.docker = docker or DockerManager()
        self.reporter = reporter or (lambda _message: None)
        self.runner = runner or run_command
        self.openclaw = openclaw or OpenClawManager(self.store, self.docker, reporter=self.reporter)
        self.set_reporter(self.reporter)

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)
        if hasattr(self.openclaw, "set_reporter"):
            self.openclaw.set_reporter(self.reporter)

    def pull_openclaw(self, version: str) -> str:
        normalized = normalize_version(version)
        self.reporter(
            f"Starting OpenClaw image preparation for version {normalized}. ClawCU will pull the official image from GHCR."
        )
        self.store.append_log(f"pull openclaw version={normalized}")
        image_tag = self.openclaw.ensure_image(normalized)
        self.store.append_log(f"prepared image {image_tag}")
        self.reporter(f"Finished preparing Docker image {image_tag}.")
        return image_tag

    def collect_providers(
        self,
        *,
        all_instances: bool = False,
        instance: str | None = None,
        path: str | None = None,
    ) -> dict[str, list[str]]:
        selected = [bool(all_instances), instance is not None, path is not None]
        if sum(selected) != 1:
            raise ValueError("Choose exactly one source: --all, --instance, or --path.")

        roots: list[tuple[str, Path, dict[str, str]]] = []
        if all_instances:
            records = self.store.list_records()
            for record in records:
                roots.append(
                    (
                        f"instance:{record.name}",
                        Path(record.datadir),
                        self._load_env_file(self.store.instance_env_path(record.name)),
                    )
                )
            local_root = self._local_openclaw_home()
            if local_root.exists():
                roots.append((f"path:{local_root}", local_root, self._load_env_file(local_root / ".env")))
        elif instance is not None:
            record = self.store.load_record(instance)
            roots.append(
                (
                    f"instance:{record.name}",
                    Path(record.datadir),
                    self._load_env_file(self.store.instance_env_path(record.name)),
                )
            )
        else:
            resolved_path = Path(path or "").expanduser().resolve()
            roots.append((f"path:{path}", resolved_path, self._load_env_file(resolved_path / ".env")))

        saved: list[str] = []
        merged: list[str] = []
        skipped: list[str] = []
        scanned: list[str] = []

        for source_label, root, env_values in roots:
            scanned.append(str(root))
            try:
                bundles = self._scan_provider_bundles(root, env_values)
            except FileNotFoundError:
                if all_instances:
                    continue
                raise
            for auth_payload, models_payload in bundles:
                base_name = self._bundle_base_name(models_payload)
                target_name, status = self._store_collected_provider_bundle(
                    base_name=base_name,
                    auth_payload=auth_payload,
                    models_payload=models_payload,
                )
                collection_label = f"{target_name} ({source_label})"
                if status == "saved":
                    saved.append(collection_label)
                elif status == "merged":
                    merged.append(collection_label)
                else:
                    skipped.append(collection_label)

        self.store.append_log(
            "provider collect "
            f"sources={','.join(scanned)} "
            f"saved={','.join(saved)} "
            f"merged={','.join(merged)} "
            f"skipped={','.join(skipped)}"
        )
        return {"saved": saved, "merged": merged, "skipped": skipped, "scanned": scanned}

    def list_providers(self) -> list[dict]:
        providers: list[dict] = []
        for name in self.store.list_provider_names():
            bundle = self.store.load_provider_bundle(name)
            model_ids = self._bundle_model_ids(bundle["models"])
            provider_name, provider_payload = self._single_provider_entry(bundle["models"])
            providers.append(
                {
                    "name": name,
                    "provider": provider_name,
                    "api_style": self._infer_api_style(provider_payload),
                    "api_key": self._bundle_api_key(bundle["auth_profiles"], bundle["models"]),
                    "endpoint": provider_payload.get("baseUrl") if isinstance(provider_payload, dict) else None,
                    "models": model_ids,
                }
            )
        return providers

    def show_provider(self, name: str) -> dict:
        bundle = self.store.load_provider_bundle(name)
        return bundle

    def remove_provider(self, name: str) -> None:
        self.store.load_provider_bundle(name)
        self.store.delete_provider(name)
        self.store.append_log(f"provider remove name={name}")

    def apply_provider(
        self,
        provider: str,
        instance: str,
        agent: str = "main",
        *,
        primary: str | None = None,
        fallbacks: list[str] | None = None,
        persist: bool = False,
    ) -> dict[str, str]:
        record = self.store.load_record(instance)
        bundle = self.store.load_provider_bundle(provider)
        agent_name = agent.strip() or "main"
        env_key = self._store_provider_api_key_in_instance_env(record.name, provider, bundle) if persist else None
        runtime_dir = Path(record.datadir) / "agents" / agent_name / "agent"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        auth_path = runtime_dir / "auth-profiles.json"
        models_path = runtime_dir / "models.json"
        merged_auth = self._merge_auth_payloads(self._load_json_file(auth_path), bundle["auth_profiles"])
        merged_models = self._merge_models_payloads(self._load_json_file(models_path), bundle["models"])
        config_path = Path(record.datadir) / "openclaw.json"
        config = self._load_json_file(config_path)
        config = self._upsert_root_provider_models_config(config, bundle["models"], env_key=env_key)
        config = self._upsert_agent_model_config(
            config,
            agent_name=agent_name,
            primary=primary,
            fallbacks=fallbacks,
        )
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # OpenClaw may hot-reload after the root config changes, so write the
        # agent runtime files last to keep the current process usable without a recreate.
        auth_path.write_text(
            json.dumps(merged_auth, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        models_path.write_text(
            json.dumps(merged_models, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.store.append_log(
            "provider apply "
            f"provider={provider} instance={record.name} agent={agent_name} "
            f"primary={primary or '-'} fallbacks={','.join(fallbacks or []) or '-'} "
            f"runtime_dir={runtime_dir}"
        )
        return {
            "provider": provider,
            "instance": record.name,
            "agent": agent_name,
            "runtime_dir": str(runtime_dir),
            "env_key": env_key or "-",
            "persist": "yes" if persist else "no",
            "primary": primary or "-",
            "fallbacks": ", ".join(fallbacks) if fallbacks else "-",
        }

    def list_provider_models(self, name: str) -> list[str]:
        bundle = self.store.load_provider_bundle(name)
        return self._bundle_model_ids(bundle["models"])

    def check_setup(self) -> list[dict[str, str | bool]]:
        checks: list[dict[str, str | bool]] = []
        docker_path = shutil.which("docker")
        if not docker_path:
            checks.append(
                {
                    "name": "docker_cli",
                    "status": "fail",
                    "ok": False,
                    "summary": "Docker CLI is not installed.",
                    "hint": "Install Docker Desktop or another Docker distribution, then rerun `clawcu setup`.",
                }
            )
            return checks

        checks.append(
            {
                "name": "docker_cli",
                "status": "ok",
                "ok": True,
                "summary": f"Docker CLI is installed at {docker_path}.",
                "hint": "",
            }
        )

        try:
            result = self.runner(["docker", "version", "--format", "{{json .Server.Version}}"])
            server_version = json.loads((result.stdout or "").strip() or '""')
            if not isinstance(server_version, str) or not server_version.strip():
                raise RuntimeError("Docker daemon did not report a server version.")
        except CommandError as exc:
            checks.append(
                {
                    "name": "docker_daemon",
                    "status": "fail",
                    "ok": False,
                    "summary": "Docker daemon is not reachable.",
                    "hint": "Start Docker Desktop (or the Docker service) and wait until `docker version` succeeds.",
                    "details": str(exc),
                }
            )
            return checks
        except (json.JSONDecodeError, RuntimeError) as exc:
            checks.append(
                {
                    "name": "docker_daemon",
                    "status": "fail",
                    "ok": False,
                    "summary": "Docker daemon returned an unexpected response.",
                    "hint": "Restart Docker and rerun `clawcu setup`. If the problem continues, check `docker version` manually.",
                    "details": str(exc),
                }
            )
            return checks

        checks.append(
            {
                "name": "docker_daemon",
                "status": "ok",
                "ok": True,
                "summary": f"Docker daemon is running (server {server_version}).",
                "hint": "",
            }
        )
        paths = self.store.paths
        checks.append(
            {
                "name": "clawcu_home",
                "status": "ok",
                "ok": True,
                "summary": f"ClawCU home directory is ready at {paths.home}.",
                "hint": "",
            }
        )
        checks.append(
            {
                "name": "clawcu_runtime_dirs",
                "status": "ok",
                "ok": True,
                "summary": (
                    "ClawCU runtime directories are ready: "
                    f"{paths.instances_dir}, {paths.providers_dir}, {paths.sources_dir}, {paths.logs_dir}, {paths.snapshots_dir}."
                ),
                "hint": "",
            }
        )
        checks.append(
            {
                "name": "openclaw_image_repo",
                "status": "ok",
                "ok": True,
                "summary": (
                    "OpenClaw image repo is configured as "
                    f"{self.get_openclaw_image_repo()}."
                ),
                "hint": "",
            }
        )
        return checks

    def get_clawcu_home(self) -> str:
        return str(self.store.paths.home)

    def set_clawcu_home(self, home: str) -> str:
        resolved = str(Path(home).expanduser().resolve())
        if not resolved.strip():
            raise ValueError("ClawCU home cannot be empty.")
        self.store.switch_home(resolved)
        self.store.set_bootstrap_home(resolved)
        self.openclaw.store = self.store
        self.openclaw.image_repo = self.store.get_openclaw_image_repo() or os.environ.get(
            "CLAWCU_OPENCLAW_IMAGE_REPO",
            getattr(self.openclaw, "image_repo", "ghcr.io/openclaw/openclaw"),
        )
        self.store.append_log(f"setup clawcu_home={resolved}")
        return resolved

    def get_openclaw_image_repo(self) -> str:
        return self.store.get_openclaw_image_repo() or getattr(
            self.openclaw,
            "image_repo",
            DEFAULT_OPENCLAW_IMAGE_REPO,
        )

    def suggest_openclaw_image_repo(self) -> str:
        configured = self.store.get_openclaw_image_repo()
        if configured:
            return configured
        env_repo = os.environ.get("CLAWCU_OPENCLAW_IMAGE_REPO")
        if isinstance(env_repo, str) and env_repo.strip():
            return env_repo.strip()
        country_code = self._detect_public_country_code()
        if country_code == "CN":
            return DEFAULT_OPENCLAW_IMAGE_REPO_CN
        return DEFAULT_OPENCLAW_IMAGE_REPO

    def set_openclaw_image_repo(self, image_repo: str) -> str:
        cleaned = image_repo.strip()
        if not cleaned:
            raise ValueError("OpenClaw image repo cannot be empty.")
        self.store.set_openclaw_image_repo(cleaned)
        self.openclaw.image_repo = cleaned
        self.store.append_log(f"setup openclaw_image_repo={cleaned}")
        return cleaned

    def _detect_public_country_code(self) -> str | None:
        endpoints = (
            ("https://ipapi.co/json/", "country_code"),
            ("https://ipinfo.io/json", "country"),
        )
        headers = {"User-Agent": f"clawcu/{clawcu_version}"}
        for url, field in endpoints:
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            raw = payload.get(field)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().upper()
        return None

    def create_openclaw(
        self,
        *,
        name: str,
        version: str,
        datadir: str | None = None,
        port: int | None = None,
        cpu: str,
        memory: str,
    ) -> InstanceRecord:
        auto_port = port is None
        self.reporter("Step 1/5: Validating options and resolving defaults. This should take a second or two.")
        spec = self._build_spec(
            name=name,
            version=version,
            datadir=datadir,
            port=port,
            cpu=cpu,
            memory=memory,
        )
        if self.store.instance_path(spec.name).exists():
            raise ValueError(f"Instance '{spec.name}' already exists.")
        container_name = container_name_for_instance(spec.name)
        if self.docker.container_status(container_name) != "missing":
            raise ValueError(
                f"Instance '{spec.name}' already exists. Docker container '{container_name}' is already present."
            )

        self.reporter(
            f"Resolved instance settings: datadir={spec.datadir}, port={spec.port}, cpu={spec.cpu}, memory={spec.memory}, auth=token."
        )
        self.store.append_log(
            f"create instance name={spec.name} version={spec.version} datadir={spec.datadir}"
        )
        self.openclaw.ensure_image(spec.version)
        datadir_path = Path(spec.datadir)
        self.reporter("Step 4/5: Preparing the local data directory and runtime metadata. This usually takes a few seconds.")
        datadir_path.mkdir(parents=True, exist_ok=True)
        history = [
            {
                "action": "create_requested",
                "timestamp": utc_now_iso(),
                "version": normalize_version(spec.version),
                "clawcu_version": clawcu_version,
                "auth_mode": spec.auth_mode,
            }
        ]
        self.reporter("Step 5/5: Starting the Docker container and checking health. This usually takes a few seconds.")
        live_record = self._start_new_instance(spec, history=history, auto_port=auto_port)
        self.reporter(self._lifecycle_summary("created", live_record))
        return live_record

    def list_instances(self, *, running_only: bool = False) -> list[InstanceRecord]:
        records = self.store.list_records()
        refreshed: list[InstanceRecord] = []
        for record in records:
            live = self._persist_live_status(record)
            if running_only and live.status != "running":
                continue
            refreshed.append(live)
        return refreshed

    def list_instance_summaries(self, *, running_only: bool = False) -> list[dict]:
        summaries: list[dict] = []
        for record in self.list_instances(running_only=running_only):
            payload = record.to_dict()
            payload.update(self._instance_provider_summary(record))
            payload["source"] = "managed"
            payload["home"] = record.datadir
            payload["snapshot"] = self._latest_snapshot_label(record)
            summaries.append(payload)
        return summaries

    def list_agent_summaries(self, *, running_only: bool = False) -> list[dict]:
        summaries: list[dict] = []
        for record in self.list_instances(running_only=running_only):
            for agent_summary in self._instance_agent_summaries(record):
                summaries.append(
                    {
                        "source": "managed",
                        "instance": record.name,
                        "home": record.datadir,
                        "service": record.service,
                        "version": record.version,
                        "port": record.port,
                        "status": record.status,
                        **agent_summary,
                    }
                )
        return summaries

    def list_local_instance_summaries(self) -> list[dict]:
        config_path = self._local_openclaw_home() / "openclaw.json"
        if not config_path.exists():
            return []
        config = self._load_json_file(config_path)
        provider_summary = self._config_provider_summary(config)
        version = self._config_version(config)
        return [
            {
                "source": "local",
                "name": "local",
                "home": str(self._local_openclaw_home()),
                "version": version,
                "port": self.DEFAULT_OPENCLAW_PORT,
                "status": "local",
                "providers": provider_summary["providers"],
                "models": provider_summary["models"],
            }
        ]

    def list_local_agent_summaries(self) -> list[dict]:
        config_path = self._local_openclaw_home() / "openclaw.json"
        if not config_path.exists():
            return []
        config = self._load_json_file(config_path)
        provider_summary = self._config_provider_summary(config)
        version = self._config_version(config)
        summaries: list[dict] = []
        for agent_summary in self._configured_agent_models(config):
            summaries.append(
                {
                    "source": "local",
                    "instance": "local",
                    "home": str(self._local_openclaw_home()),
                    "service": "openclaw",
                    "version": version,
                    "port": self.DEFAULT_OPENCLAW_PORT,
                    "status": "local",
                    "providers": provider_summary["providers"],
                    "models": provider_summary["models"],
                    **agent_summary,
                }
            )
        return summaries

    def inspect_instance(self, name: str) -> dict:
        record = self._persist_live_status(self.store.load_record(name))
        inspection = self.docker.inspect_container(record.container_name)
        return {
            "instance": record.to_dict(),
            "snapshots": self._snapshot_summary(record),
            "container": inspection,
        }

    def dashboard_url(self, name: str) -> str:
        record = self._persist_live_status(self.store.load_record(name))
        token = self._gateway_token(record)
        base_url = f"http://127.0.0.1:{record.port}/"
        if token:
            return f"{base_url}#token={urllib.parse.quote(token, safe='')}"
        return base_url

    def token(self, name: str) -> str:
        record = self._persist_live_status(self.store.load_record(name))
        token = self._gateway_token(record)
        if token:
            return token
        raise ValueError(f"Instance '{record.name}' does not have a dashboard token configured.")

    def set_instance_env(self, name: str, assignments: list[str]) -> dict[str, object]:
        if not assignments:
            raise ValueError("Please provide at least one KEY=VALUE assignment.")

        record = self.store.load_record(name)
        env_path = self.store.instance_env_path(record.name)
        env_values = self._load_env_file(env_path)
        updated_keys: list[str] = []

        for assignment in assignments:
            if "=" not in assignment:
                raise ValueError(f"Invalid assignment '{assignment}'. Use KEY=VALUE.")
            key, value = assignment.split("=", 1)
            key = key.strip()
            if not self._is_valid_env_key(key):
                raise ValueError(
                    f"Invalid environment variable name '{key}'. Use letters, numbers, and underscores, and do not start with a number."
                )
            if "\n" in value or "\r" in value:
                raise ValueError(f"Environment variable '{key}' cannot contain newlines.")
            env_values[key] = value
            updated_keys.append(key)

        env_path.write_text(self._dump_env_file(env_values), encoding="utf-8")
        self.store.append_log(
            f"setenv instance={record.name} keys={','.join(updated_keys)} path={env_path}"
        )
        return {
            "instance": record.name,
            "path": str(env_path),
            "updated_keys": updated_keys,
            "status": record.status,
        }

    def get_instance_env(self, name: str) -> dict[str, object]:
        record = self.store.load_record(name)
        env_path = self.store.instance_env_path(record.name)
        env_values = self._load_env_file(env_path)
        return {
            "instance": record.name,
            "path": str(env_path),
            "values": env_values,
            "status": record.status,
        }

    def unset_instance_env(self, name: str, keys: list[str]) -> dict[str, object]:
        if not keys:
            raise ValueError("Please provide at least one environment variable name.")

        record = self.store.load_record(name)
        env_path = self.store.instance_env_path(record.name)
        env_values = self._load_env_file(env_path)
        removed_keys: list[str] = []

        for key in keys:
            clean_key = key.strip()
            if not self._is_valid_env_key(clean_key):
                raise ValueError(
                    f"Invalid environment variable name '{clean_key}'. Use letters, numbers, and underscores, and do not start with a number."
                )
            if clean_key in env_values:
                env_values.pop(clean_key, None)
                removed_keys.append(clean_key)

        env_path.write_text(self._dump_env_file(env_values), encoding="utf-8")
        self.store.append_log(
            f"unsetenv instance={record.name} keys={','.join(removed_keys)} path={env_path}"
        )
        return {
            "instance": record.name,
            "path": str(env_path),
            "removed_keys": removed_keys,
            "status": record.status,
        }

    def _instance_provider_summary(self, record: InstanceRecord) -> dict[str, str]:
        config_path = Path(record.datadir) / "openclaw.json"
        config = self._load_json_file(config_path)
        summary = self._config_provider_summary(config)
        if summary["providers"] != "-" or summary["models"] != "-":
            return summary

        provider_names: list[str] = []
        model_names: list[str] = []
        for agent_name in self._managed_agent_names(Path(record.datadir)):
            agent_summary = self._agent_runtime_provider_summary(Path(record.datadir), agent_name)
            provider_names.extend(self._split_summary_values(agent_summary["providers"]))
            model_names.extend(self._split_summary_values(agent_summary["models"]))
        return self._summary_from_lists(provider_names, model_names)

    def _config_provider_summary(self, config: dict) -> dict[str, str]:
        providers = self._configured_provider_names(config)
        models = self._configured_model_names(config)
        return {
            "providers": ", ".join(providers) if providers else "-",
            "models": ", ".join(models) if models else "-",
        }

    def _instance_agent_summaries(self, record: InstanceRecord) -> list[dict[str, str]]:
        config_path = Path(record.datadir) / "openclaw.json"
        config = self._load_json_file(config_path)
        root_provider_summary = self._config_provider_summary(config)
        configured = self._configured_agent_models(config)
        if configured:
            summaries: list[dict[str, str]] = []
            for agent_summary in configured:
                runtime_summary = self._agent_runtime_provider_summary(Path(record.datadir), agent_summary["agent"])
                effective_summary = runtime_summary
                if runtime_summary["providers"] == "-" and runtime_summary["models"] == "-":
                    effective_summary = root_provider_summary
                summaries.append({**effective_summary, **agent_summary})
            return summaries

        agent_names = self._managed_agent_names(Path(record.datadir))
        if not agent_names:
            return []

        default_primary, default_fallbacks = self._configured_default_agent_model(config)
        return [
            {
                "agent": agent_name,
                **self._agent_runtime_provider_summary(Path(record.datadir), agent_name),
                "primary": default_primary,
                "fallbacks": default_fallbacks,
            }
            for agent_name in agent_names
        ]

    def _agent_runtime_provider_summary(self, datadir: Path, agent_name: str) -> dict[str, str]:
        runtime_dir = datadir / "agents" / agent_name / "agent"
        models_payload = self._load_json_file(runtime_dir / "models.json")
        return self._config_provider_summary({"models": models_payload})

    def _summary_from_lists(self, providers: list[str], models: list[str]) -> dict[str, str]:
        provider_values = sorted(dict.fromkeys(item for item in providers if item and item != "-"))
        model_values = sorted(dict.fromkeys(item for item in models if item and item != "-"))
        return {
            "providers": ", ".join(provider_values) if provider_values else "-",
            "models": ", ".join(model_values) if model_values else "-",
        }

    def _split_summary_values(self, value: str) -> list[str]:
        if not value or value == "-":
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    def _is_valid_env_key(self, key: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key))

    def _load_env_file(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            values[key] = value
        return values

    def _dump_env_file(self, values: dict[str, str]) -> str:
        lines = [f"{key}={values[key]}" for key in sorted(values)]
        return ("\n".join(lines) + "\n") if lines else ""

    def _config_version(self, config: dict) -> str:
        version = config.get("meta", {}).get("lastTouchedVersion")
        if isinstance(version, str) and version.strip():
            return version.strip()
        return "-"

    def _local_openclaw_home(self) -> Path:
        return Path.home() / ".openclaw"

    def approve_pairing(self, name: str, request_id: str | None = None) -> str:
        record = self._persist_live_status(self.store.load_record(name))
        selected_request_id = request_id or self._latest_pending_request_id(record)
        if not selected_request_id:
            raise ValueError(
                f"Instance '{record.name}' has no pending pairing requests."
            )
        env_values = self._load_env_file(self.store.instance_env_path(record.name))
        self.reporter(
            f"Approving pairing request {selected_request_id} for instance '{record.name}'."
        )
        self.docker.exec_in_container(
            record.container_name,
            ["node", "openclaw.mjs", "devices", "approve", selected_request_id],
            env=env_values,
        )
        self.store.append_log(
            f"approve pairing instance={record.name} request_id={selected_request_id}"
        )
        return selected_request_id

    def configure_instance(self, name: str, extra_args: list[str] | None = None) -> None:
        record = self._persist_live_status(self.store.load_record(name))
        command = ["node", "openclaw.mjs", "configure", *(extra_args or [])]
        env_values = self._load_env_file(self.store.instance_env_path(record.name))
        self.store.append_log(
            f"configure instance name={record.name} args={' '.join(extra_args or [])}".strip()
        )
        self.docker.exec_in_container_interactive(record.container_name, command, env=env_values)

    def exec_instance(self, name: str, command: list[str]) -> None:
        if not command:
            raise ValueError("Please provide a command to run inside the instance.")
        record = self._persist_live_status(self.store.load_record(name))
        env_values = self._load_env_file(self.store.instance_env_path(record.name))
        self.store.append_log(
            f"exec instance name={record.name} command={' '.join(command)}"
        )
        self.docker.exec_in_container_interactive(record.container_name, command, env=env_values)

    def tui_instance(self, name: str, *, agent: str = "main") -> None:
        agent_name = (agent or "main").strip() or "main"
        record = self._persist_live_status(self.store.load_record(name))
        pending_request_id = self._latest_pending_request_id(record)
        if pending_request_id:
            self.approve_pairing(name, request_id=pending_request_id)
        else:
            self.reporter(
                f"No pending pairing request was found for instance '{record.name}'. Launching TUI directly."
            )

        command = ["openclaw", "tui"]
        if agent_name != "main":
            command.extend(["--agent", agent_name])
        self.exec_instance(name, command)

    def start_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        try:
            self.docker.start_container(record.container_name)
        except Exception as exc:
            failed = updated_record(
                record,
                status="start_failed",
                last_error=str(exc),
            )
            failed.history.append(
                {
                    "action": "start_failed",
                    "timestamp": utc_now_iso(),
                    "error": str(exc),
                }
            )
            self.store.save_record(failed)
            raise RuntimeError(f"Failed to start instance '{record.name}': {exc}") from exc
        self.store.append_log(f"start instance name={record.name}")
        return self._persist_live_status(record)

    def stop_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        self.docker.stop_container(record.container_name)
        self.store.append_log(f"stop instance name={record.name}")
        return self._persist_live_status(record)

    def restart_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        self.docker.restart_container(record.container_name)
        self.store.append_log(f"restart instance name={record.name}")
        return self._persist_live_status(record)

    def retry_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        if record.status != "create_failed":
            raise ValueError(
                f"Instance '{name}' is in status '{record.status}'. Only create_failed instances can be retried."
            )

        self.reporter("Step 1/4: Loading the failed instance record and validating retry state.")
        self.reporter(
            f"Retrying instance '{record.name}' with version {record.version}, datadir={record.datadir}, port={record.port}, cpu={record.cpu}, memory={record.memory}."
        )
        self.store.append_log(f"retry instance name={record.name} version={record.version}")
        self.reporter("Step 2/4: Making sure the requested OpenClaw image is available.")
        self.openclaw.ensure_image(record.version)
        self.reporter("Step 3/4: Cleaning up any leftover Docker container from the failed attempt.")
        self.docker.remove_container(record.container_name, missing_ok=True)
        self.reporter("Step 4/4: Recreating the Docker container. This usually takes a few seconds.")

        spec = InstanceSpec(
            service=record.service,
            name=record.name,
            version=record.version,
            datadir=record.datadir,
            port=record.port,
            cpu=record.cpu,
            memory=record.memory,
            auth_mode=self._supported_auth_mode(record.auth_mode),
        )
        history = copy.deepcopy(record.history)
        history.append(
            {
                "action": "retry_requested",
                "timestamp": utc_now_iso(),
                "version": record.version,
                "from_status": record.status,
            }
        )
        live_record = self._start_new_instance(spec, history=history, auto_port=True)
        self.reporter(self._lifecycle_summary("retried", live_record))
        return live_record

    def recreate_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        effective_auth_mode = self._supported_auth_mode(record.auth_mode)
        self.reporter(
            f"Recreating instance '{record.name}' (version {record.version}, port {record.port}, auth={effective_auth_mode})."
        )
        self.store.append_log(f"recreate instance name={record.name} version={record.version}")
        self.openclaw.ensure_image(record.version)
        self.docker.remove_container(record.container_name, missing_ok=True)

        spec = InstanceSpec(
            service=record.service,
            name=record.name,
            version=record.version,
            datadir=record.datadir,
            port=record.port,
            cpu=record.cpu,
            memory=record.memory,
            auth_mode=effective_auth_mode,
        )
        history = copy.deepcopy(record.history)
        history.append(
            {
                "action": "recreate_requested",
                "timestamp": utc_now_iso(),
                "version": record.version,
                "from_status": record.status,
                "clawcu_version": clawcu_version,
                "auth_mode": effective_auth_mode,
            }
        )
        live_record = self._start_new_instance(spec, history=history, auto_port=False)
        self.reporter(self._lifecycle_summary("recreated", live_record))
        return live_record

    def upgrade_instance(self, name: str, *, version: str) -> InstanceRecord:
        record = self.store.load_record(name)
        target_version = normalize_version(version)
        if target_version == record.version:
            raise ValueError(f"Instance '{name}' is already on version {target_version}.")

        self.reporter(
            f"Step 1/4: Preparing an upgrade plan for '{record.name}'. "
            "This should take a second or two."
        )
        env_path = self.store.instance_env_path(record.name)
        self.reporter(
            "Step 2/4: Creating a safety snapshot for the data directory and instance env. "
            "This usually takes a few seconds."
        )
        snapshot_dir = self.store.create_snapshot(
            record.name,
            Path(record.datadir),
            f"upgrade-to-{target_version}",
            env_path=env_path,
        )
        self.reporter(f"Created snapshot: {snapshot_dir}")
        self.store.append_log(
            f"upgrade instance name={record.name} from={record.version} to={target_version} snapshot={snapshot_dir}"
        )

        try:
            self.reporter(
                f"Step 3/4: Preparing OpenClaw {target_version}. "
                "This may take a while if the image needs to be pulled or built."
            )
            self.openclaw.ensure_image(target_version)
        except Exception as exc:
            record.history.append(
                {
                    "action": "upgrade_failed",
                    "timestamp": utc_now_iso(),
                    "from_version": record.version,
                    "to_version": target_version,
                    "snapshot_dir": str(snapshot_dir),
                    "error": str(exc),
                    "phase": "image_build",
                }
            )
            self.store.save_record(record)
            raise RuntimeError(
                f"Failed to prepare OpenClaw {target_version}. Existing instance was left untouched."
            ) from exc

        previous = copy.deepcopy(record)
        upgraded = updated_record(
            record,
            version=target_version,
            upstream_ref=upstream_ref_for_version(target_version),
            image_tag=image_tag_for_version(target_version),
            status="upgrading",
        )
        try:
            self.reporter(
                f"Step 4/4: Recreating the container on OpenClaw {target_version} "
                "with the existing data directory."
            )
            self.docker.remove_container(previous.container_name, missing_ok=True)
            self._run_container(upgraded)
            upgraded = self._wait_for_service_readiness(self._persist_live_status(upgraded))
        except Exception as exc:
            rollback_error = None
            self.reporter(
                f"Upgrade failed while starting {target_version}. "
                f"Trying to restore {previous.version} from the snapshot."
            )
            try:
                self.docker.remove_container(previous.container_name, missing_ok=True)
                if snapshot_dir.exists():
                    self.store.restore_snapshot(
                        snapshot_dir,
                        Path(previous.datadir),
                        env_path=env_path,
                    )
                self._run_container(previous)
            except Exception as nested_exc:
                rollback_error = nested_exc

            previous.history.append(
                {
                    "action": "upgrade_failed",
                    "timestamp": utc_now_iso(),
                    "from_version": previous.version,
                    "to_version": target_version,
                    "snapshot_dir": str(snapshot_dir),
                    "error": str(exc),
                    "rollback_error": str(rollback_error) if rollback_error else None,
                    "phase": "container_recreate",
                }
            )
            previous.status = self.docker.container_status(previous.container_name)
            previous.updated_at = utc_now_iso()
            self.store.save_record(previous)
            if rollback_error:
                raise RuntimeError(
                    f"Upgrade to {target_version} failed and automatic rollback also failed: {rollback_error}"
                ) from exc
            raise RuntimeError(
                f"Upgrade to {target_version} failed. Rolled back to {previous.version}."
            ) from exc

        upgraded.history.append(
            {
                "action": "upgrade",
                "timestamp": utc_now_iso(),
                "from_version": previous.version,
                "to_version": target_version,
                "snapshot_dir": str(snapshot_dir),
            }
        )
        self.store.save_record(upgraded)
        self.reporter(
            f"Upgrade snapshot retained at {snapshot_dir}. "
            f"Run 'clawcu rollback {upgraded.name}' if you want to restore {previous.version}."
        )
        self.reporter(self._lifecycle_summary("upgraded", upgraded))
        return upgraded

    def rollback_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        transition = self._latest_transition(record)
        previous_version = normalize_version(transition["from_version"])
        restore_from = transition.get("snapshot_dir")

        self.reporter(
            f"Step 1/4: Preparing to roll back '{record.name}' from {record.version} to {previous_version}. "
            "This should take a second or two."
        )
        self.store.append_log(
            f"rollback instance name={record.name} from={record.version} to={previous_version}"
        )
        self.reporter(
            f"Step 2/4: Preparing OpenClaw {previous_version}. "
            "This may take a while if the image is not available locally."
        )
        self.openclaw.ensure_image(previous_version)
        env_path = self.store.instance_env_path(record.name)
        self.reporter(
            "Step 3/4: Saving the current state and restoring the previous snapshot. "
            "This usually takes a few seconds."
        )
        current_snapshot = self.store.create_snapshot(
            record.name,
            Path(record.datadir),
            f"rollback-from-{record.version}",
            env_path=env_path,
        )
        self.reporter(f"Created rollback safety snapshot: {current_snapshot}")

        self.docker.remove_container(record.container_name, missing_ok=True)
        if restore_from and Path(restore_from).exists():
            self.reporter(f"Restoring snapshot: {restore_from}")
            self.store.restore_snapshot(
                Path(restore_from),
                Path(record.datadir),
                env_path=env_path,
            )

        rolled = updated_record(
            record,
            version=previous_version,
            upstream_ref=upstream_ref_for_version(previous_version),
            image_tag=image_tag_for_version(previous_version),
            status="rolling-back",
        )
        rolled.history.append(
            {
                "action": "rollback",
                "timestamp": utc_now_iso(),
                "from_version": record.version,
                "to_version": previous_version,
                "snapshot_dir": str(current_snapshot),
                "restored_snapshot": restore_from,
            }
        )
        self.reporter(
            f"Step 4/4: Starting OpenClaw {previous_version} "
            "with the restored data directory and env file."
        )
        self._run_container(rolled)
        rolled = self._wait_for_service_readiness(self._persist_live_status(rolled))
        self.store.save_record(rolled)
        if restore_from:
            self.reporter(
                f"Restored snapshot {restore_from}. "
                f"The data directory and instance env were rolled back together."
            )
        self.reporter(f"Rollback safety snapshot retained at {current_snapshot}.")
        self.reporter(self._lifecycle_summary("rolled_back", rolled))
        return rolled

    def clone_instance(
        self,
        source_name: str,
        *,
        name: str,
        datadir: str | None = None,
        port: int | None = None,
    ) -> InstanceRecord:
        self.reporter("Step 1/5: Validating the source instance and resolving clone defaults. This should take a second or two.")
        source = self.store.load_record(source_name)
        clone_spec = self._build_spec(
            name=name,
            version=source.version,
            datadir=datadir,
            port=port,
            cpu=source.cpu,
            memory=source.memory,
        )
        if self.store.instance_path(clone_spec.name).exists():
            raise ValueError(f"Instance '{clone_spec.name}' already exists.")
        container_name = container_name_for_instance(clone_spec.name)
        if self.docker.container_status(container_name) != "missing":
            raise ValueError(
                f"Instance '{clone_spec.name}' already exists. Docker container '{container_name}' is already present."
            )

        target_dir = Path(clone_spec.datadir)
        if target_dir.exists():
            raise ValueError(f"Target datadir '{target_dir}' already exists.")
        source_env_path = self.store.instance_env_path(source.name)
        target_env_path = self.store.instance_env_path(clone_spec.name)
        try:
            self.reporter(
                f"Resolved clone settings: datadir={clone_spec.datadir}, port={clone_spec.port}, cpu={clone_spec.cpu}, memory={clone_spec.memory}."
            )
            self.reporter("Step 2/5: Copying the source data directory into a new experiment directory. This can take a while for larger instances.")
            shutil.copytree(source.datadir, target_dir)
            if source_env_path.exists():
                self.reporter("Step 3/5: Copying the instance environment variables. This usually takes a second or two.")
                target_env_path.write_text(source_env_path.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.reporter("Step 3/5: No instance environment file was found on the source instance. Skipping env copy.")
            self.reporter("Step 4/5: Making sure the requested OpenClaw image is available.")
            self.openclaw.ensure_image(clone_spec.version)
            self.reporter("Step 5/5: Starting the cloned Docker container and checking health. This usually takes a few seconds.")
            record = self._start_new_instance(
                clone_spec,
                history=[
                    {
                        "action": "cloned",
                        "timestamp": utc_now_iso(),
                        "from_instance": source.name,
                        "to_version": source.version,
                    }
                ],
                auto_port=port is None,
            )
        except Exception:
            self.docker.remove_container(container_name, missing_ok=True)
            self.store.delete_record(clone_spec.name)
            if target_env_path.exists():
                target_env_path.unlink()
            if target_dir.exists():
                shutil.rmtree(target_dir)
            raise
        self.store.append_log(
            f"clone instance source={source.name} target={record.name} datadir={record.datadir}"
        )
        return record

    def stream_logs(self, name: str, *, follow: bool = False) -> None:
        record = self.store.load_record(name)
        self.docker.stream_logs(record.container_name, follow=follow)

    def remove_instance(self, name: str, *, delete_data: bool = False) -> None:
        record = self.store.load_record(name)
        self.docker.remove_container(record.container_name, missing_ok=True)
        if delete_data and Path(record.datadir).exists():
            shutil.rmtree(record.datadir)
        self.store.delete_record(record.name)
        self.store.append_log(
            f"remove instance name={record.name} delete_data={'yes' if delete_data else 'no'}"
        )

    def _build_spec(
        self,
        *,
        name: str,
        version: str,
        datadir: str | None,
        port: int | None,
        cpu: str,
        memory: str,
    ) -> InstanceSpec:
        validated_name = validate_name(name)
        resolved_datadir = resolve_datadir(datadir) if datadir else self._default_datadir(validated_name)
        resolved_port = validate_port(port) if port is not None else self._next_available_port()
        return InstanceSpec(
            service="openclaw",
            name=validated_name,
            version=normalize_version(version),
            datadir=resolved_datadir,
            port=resolved_port,
            cpu=validate_cpu(cpu),
            memory=validate_memory(memory),
            auth_mode="token",
        )

    def _default_datadir(self, name: str) -> str:
        return str((self.store.paths.home / name).resolve())

    def _supported_auth_mode(self, auth_mode: str | None) -> str:
        if (auth_mode or "token").strip().lower() != "token":
            self.reporter(
                "OpenClaw v0.0.1 requires token auth for lan binding. ClawCU will recreate this instance with token auth."
            )
        return "token"

    def _next_available_port(self, *, start_port: int | None = None) -> int:
        port = start_port if start_port is not None else self.DEFAULT_OPENCLAW_PORT
        for _ in range(self.PORT_SEARCH_LIMIT):
            if self._is_port_available(port):
                return port
            port += self.PORT_SEARCH_STEP
        raise RuntimeError("Could not find a free OpenClaw port in the configured search range.")

    def _is_port_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def _start_new_instance(
        self,
        spec: InstanceSpec,
        *,
        history: list[dict],
        auto_port: bool,
    ) -> InstanceRecord:
        current_spec = spec
        current_history = copy.deepcopy(history)
        while True:
            record = build_instance_record(
                current_spec,
                status="creating",
                history=copy.deepcopy(current_history),
            )
            self.store.save_record(record)
            try:
                self._configure_gateway(record)
                self._run_container(record)
            except Exception as exc:
                failure = updated_record(
                    record,
                    status="create_failed",
                    last_error=str(exc),
                )
                failure.history.append(
                    {
                        "action": "create_failed",
                        "timestamp": utc_now_iso(),
                        "version": record.version,
                        "port": record.port,
                        "error": str(exc),
                    }
                )
                self.store.save_record(failure)
                self.docker.remove_container(record.container_name, missing_ok=True)
                if not isinstance(exc, CommandError) or not auto_port or not self._is_port_bind_error(exc):
                    raise RuntimeError(f"Failed to create instance '{record.name}': {exc}") from exc
                next_port = self._next_available_port(start_port=current_spec.port + self.PORT_SEARCH_STEP)
                self.reporter(
                    f"Port {current_spec.port} was claimed before Docker could bind it. Retrying with port {next_port}."
                )
                current_history = copy.deepcopy(failure.history)
                current_spec = replace(current_spec, port=next_port)
                continue

            record.history.append(
                {
                    "action": "created",
                    "timestamp": utc_now_iso(),
                    "version": record.version,
                    "port": record.port,
                }
            )
            live_record = self._persist_live_status(record)
            try:
                return self._wait_for_service_readiness(live_record)
            except RuntimeError as exc:
                current = self.store.load_record(record.name)
                failed = updated_record(current, last_error=str(exc))
                failed.history.append(
                    {
                        "action": "startup_failed",
                        "timestamp": utc_now_iso(),
                        "version": current.version,
                        "port": current.port,
                        "status": current.status,
                        "error": str(exc),
                    }
                )
                self.store.save_record(failed)
                raise

    def _wait_for_service_readiness(self, record: InstanceRecord) -> InstanceRecord:
        pending_statuses = {"starting", "created"}
        if record.status == "running":
            return record

        self.reporter(
            f"Waiting for OpenClaw to become ready on port {record.port}. "
            f"You can watch Docker state with 'docker ps --filter name={record.container_name}' "
            f"or inspect details with 'clawcu inspect {record.name}' or 'clawcu logs {record.name}'."
        )
        start_time = time.monotonic()
        last_reported_status: str | None = None
        last_reported_bucket = -1
        current = record

        while True:
            current = self._persist_live_status(current)
            if self._host_healthcheck_ready(current):
                ready = updated_record(current, status="running", last_error=None)
                self.store.save_record(ready)
                self.reporter(
                    f"OpenClaw health endpoint is responding on http://127.0.0.1:{ready.port}/healthz. Marking the instance as ready."
                )
                return ready
            if current.status == "running":
                return current
            if current.status not in pending_statuses:
                message = (
                    f"Instance '{current.name}' did not become ready. Current status is '{current.status}'. "
                    f"Use 'clawcu inspect {current.name}' or 'clawcu logs {current.name}' for details."
                )
                self.store.save_record(updated_record(current, last_error=message))
                raise RuntimeError(message)

            elapsed = int(time.monotonic() - start_time)
            progress_bucket = (
                int(elapsed // self.STARTUP_PROGRESS_INTERVAL_SECONDS)
                if self.STARTUP_PROGRESS_INTERVAL_SECONDS > 0
                else elapsed
            )
            if current.status != last_reported_status or progress_bucket != last_reported_bucket:
                self.reporter(
                    f"OpenClaw is still {current.status} on port {current.port} after {elapsed}s. "
                    "Continuing to wait for readiness."
                )
                last_reported_status = current.status
                last_reported_bucket = progress_bucket

            time.sleep(self.STARTUP_POLL_INTERVAL_SECONDS)

    def _host_healthcheck_ready(self, record: InstanceRecord) -> bool:
        url = f"http://127.0.0.1:{record.port}/healthz"
        request = urllib.request.Request(url, headers={"User-Agent": f"clawcu/{clawcu_version}"})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 400
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return False

    def _scan_provider_bundles(self, root: Path, env_values: dict[str, str] | None = None) -> list[tuple[dict, dict]]:
        root = root.expanduser().resolve()
        agents_dir = root / "agents"
        env_values = env_values or {}

        bundles: list[tuple[dict, dict]] = []
        root_provider_names: set[str] = set()
        root_config_path = root / "openclaw.json"
        root_config = self._load_json_file(root_config_path)
        root_models = root_config.get("models", {})
        root_providers = root_models.get("providers", {}) if isinstance(root_models, dict) else {}
        root_auth = root_config.get("auth", {})
        root_auth_profiles = root_auth.get("profiles", {}) if isinstance(root_auth, dict) else {}
        if isinstance(root_providers, dict):
            for provider_name, provider_payload in root_providers.items():
                if not isinstance(provider_name, str) or not isinstance(provider_payload, dict):
                    continue
                resolved_provider_payload = self._resolve_env_placeholders(provider_payload, env_values)
                if not isinstance(resolved_provider_payload, dict):
                    continue
                resolved_root_auth_payload = self._resolve_env_placeholders(
                    {"profiles": copy.deepcopy(root_auth_profiles)}
                    if isinstance(root_auth_profiles, dict)
                    else {},
                    env_values,
                )
                root_provider_names.add(provider_name)
                bundles.append(
                    (
                        self._build_auth_bundle_for_provider(
                            resolved_root_auth_payload if isinstance(resolved_root_auth_payload, dict) else {},
                            provider_name,
                            resolved_provider_payload,
                        ),
                        {
                            "providers": {
                                provider_name: copy.deepcopy(resolved_provider_payload),
                            }
                        },
                    )
                )

        if root_provider_names:
            return bundles

        if not agents_dir.exists():
            raise FileNotFoundError(
                f"OpenClaw data directory '{root}' does not declare providers in openclaw.json or contain an agents directory."
            )

        return bundles

    def _resolve_env_placeholders(self, value, env_values: dict[str, str]):
        if isinstance(value, dict):
            return {key: self._resolve_env_placeholders(item, env_values) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_env_placeholders(item, env_values) for item in value]
        if isinstance(value, str):
            pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

            def replace(match: re.Match[str]) -> str:
                key = match.group(1)
                return env_values.get(key, match.group(0))

            return pattern.sub(replace, value)
        return value

    def _build_auth_bundle_for_provider(
        self,
        auth_payload: dict,
        provider_name: str,
        provider_payload: dict | None = None,
    ) -> dict:
        profiles = auth_payload.get("profiles", {})
        provider_api_key = None
        if isinstance(provider_payload, dict):
            api_key = provider_payload.get("apiKey")
            if isinstance(api_key, str) and api_key.strip():
                provider_api_key = api_key.strip()

        filtered_profiles = {
            name: copy.deepcopy(profile)
            for name, profile in profiles.items()
            if isinstance(profile, dict) and profile.get("provider") == provider_name
        }
        for profile in filtered_profiles.values():
            if not isinstance(profile, dict):
                continue
            mode = profile.get("mode")
            if isinstance(mode, str) and mode.strip() and "type" not in profile:
                profile["type"] = mode.strip()
            if provider_api_key and profile.get("type") == "api_key":
                existing_key = profile.get("key")
                existing_api_key = profile.get("apiKey")
                if not (isinstance(existing_key, str) and existing_key.strip()) and not (
                    isinstance(existing_api_key, str) and existing_api_key.strip()
                ):
                    profile["key"] = provider_api_key

        if not filtered_profiles and provider_api_key:
            synthesized_name = f"{provider_name}:default"
            filtered_profiles[synthesized_name] = {
                "type": "api_key",
                "provider": provider_name,
                "key": provider_api_key,
            }

        profile_names = set(filtered_profiles)
        filtered_last_good = {
            name: selected
            for name, selected in auth_payload.get("lastGood", {}).items()
            if name == provider_name or selected in profile_names
        }
        if not filtered_last_good and profile_names:
            filtered_last_good = {provider_name: next(iter(sorted(profile_names)))}
        filtered_usage = {
            name: usage
            for name, usage in auth_payload.get("usageStats", {}).items()
            if name in profile_names
        }
        result: dict[str, object] = {"profiles": filtered_profiles}
        if "version" in auth_payload:
            result["version"] = auth_payload["version"]
        if filtered_last_good:
            result["lastGood"] = filtered_last_good
        if filtered_usage:
            result["usageStats"] = filtered_usage
        return result

    def _merge_auth_payloads(self, existing: dict, incoming: dict) -> dict:
        merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        merged_profiles = merged.setdefault("profiles", {})
        incoming_profiles = incoming.get("profiles", {})
        if not isinstance(merged_profiles, dict):
            merged_profiles = {}
            merged["profiles"] = merged_profiles
        if isinstance(incoming_profiles, dict):
            merged_profiles.update(copy.deepcopy(incoming_profiles))

        merged_last_good = merged.setdefault("lastGood", {})
        incoming_last_good = incoming.get("lastGood", {})
        if not isinstance(merged_last_good, dict):
            merged_last_good = {}
            merged["lastGood"] = merged_last_good
        if isinstance(incoming_last_good, dict):
            merged_last_good.update(copy.deepcopy(incoming_last_good))

        merged_usage = merged.setdefault("usageStats", {})
        incoming_usage = incoming.get("usageStats", {})
        if not isinstance(merged_usage, dict):
            merged_usage = {}
            merged["usageStats"] = merged_usage
        if isinstance(incoming_usage, dict):
            merged_usage.update(copy.deepcopy(incoming_usage))

        if "version" not in merged and "version" in incoming:
            merged["version"] = incoming["version"]
        return merged

    def _merge_models_payloads(self, existing: dict, incoming: dict) -> dict:
        merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        merged_providers = merged.setdefault("providers", {})
        incoming_providers = incoming.get("providers", {})
        if not isinstance(merged_providers, dict):
            merged_providers = {}
            merged["providers"] = merged_providers
        if not isinstance(incoming_providers, dict):
            return merged

        for provider_name, incoming_payload in incoming_providers.items():
            if not isinstance(provider_name, str) or not isinstance(incoming_payload, dict):
                continue
            existing_payload = merged_providers.get(provider_name)
            if not isinstance(existing_payload, dict):
                merged_providers[provider_name] = copy.deepcopy(incoming_payload)
                continue
            merged_providers[provider_name] = self._merge_provider_payload(existing_payload, incoming_payload)
        return merged

    def _merge_provider_payload(self, existing_payload: dict, incoming_payload: dict) -> dict:
        merged_payload = copy.deepcopy(existing_payload)
        for key, value in incoming_payload.items():
            if key != "models":
                merged_payload[key] = copy.deepcopy(value)
                continue
            existing_models = merged_payload.get("models", [])
            incoming_models = value if isinstance(value, list) else []
            if not isinstance(existing_models, list):
                existing_models = []
            merged_payload["models"] = self._merge_model_lists(existing_models, incoming_models)
        return merged_payload

    def _merge_model_lists(self, existing_models: list, incoming_models: list) -> list[dict]:
        merged_models = [copy.deepcopy(model) for model in existing_models if isinstance(model, dict)]
        index_by_id = {
            model.get("id"): idx
            for idx, model in enumerate(merged_models)
            if isinstance(model.get("id"), str)
        }
        for model in incoming_models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if isinstance(model_id, str) and model_id in index_by_id:
                merged_models[index_by_id[model_id]].update(copy.deepcopy(model))
                continue
            merged_models.append(copy.deepcopy(model))
            if isinstance(model_id, str):
                index_by_id[model_id] = len(merged_models) - 1
        return merged_models

    def _upsert_agent_model_config(
        self,
        config: dict,
        *,
        agent_name: str,
        primary: str | None,
        fallbacks: list[str] | None,
    ) -> dict:
        if primary is None and fallbacks is None:
            return config

        merged = copy.deepcopy(config) if isinstance(config, dict) else {}
        agents_config = merged.setdefault("agents", {})
        if not isinstance(agents_config, dict):
            agents_config = {}
            merged["agents"] = agents_config

        listed_agents = agents_config.setdefault("list", [])
        if not isinstance(listed_agents, list):
            listed_agents = []
            agents_config["list"] = listed_agents

        target_agent: dict | None = None
        for item in listed_agents:
            if not isinstance(item, dict):
                continue
            agent_id = item.get("id") or item.get("name")
            if isinstance(agent_id, str) and agent_id.strip() == agent_name:
                target_agent = item
                break

        if target_agent is None:
            target_agent = {"id": agent_name}
            listed_agents.append(target_agent)

        model_config = target_agent.setdefault("model", {})
        if not isinstance(model_config, dict):
            model_config = {}
            target_agent["model"] = model_config

        if primary is not None:
            model_config["primary"] = primary
        if fallbacks is not None:
            model_config["fallbacks"] = fallbacks
        return merged

    def _upsert_root_provider_models_config(
        self,
        config: dict,
        models_payload: dict,
        *,
        env_key: str | None = None,
    ) -> dict:
        providers_payload = models_payload.get("providers", {})
        if not isinstance(providers_payload, dict):
            return config

        merged = copy.deepcopy(config) if isinstance(config, dict) else {}
        models_config = merged.setdefault("models", {})
        if not isinstance(models_config, dict):
            models_config = {}
            merged["models"] = models_config
        providers = models_config.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            models_config["providers"] = providers

        for provider_name, provider_payload in providers_payload.items():
            if isinstance(provider_name, str) and isinstance(provider_payload, dict):
                rendered_payload = copy.deepcopy(provider_payload)
                if env_key:
                    api_key = rendered_payload.get("apiKey")
                    if isinstance(api_key, str) and api_key.strip():
                        rendered_payload["apiKey"] = f"${{{env_key}}}"
                else:
                    rendered_payload.pop("apiKey", None)
                providers[provider_name] = rendered_payload
        return merged

    def _provider_env_key(self, provider_name: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", provider_name.strip()).strip("_").upper()
        return f"CLAWCU_PROVIDER_{normalized}_API_KEY"

    def _store_provider_api_key_in_instance_env(
        self,
        instance_name: str,
        provider_name: str,
        bundle: dict,
    ) -> str | None:
        api_key = self._bundle_api_key(bundle["auth_profiles"], bundle["models"])
        if not isinstance(api_key, str) or not api_key.strip():
            return None

        env_key = self._provider_env_key(provider_name)
        env_path = self.store.instance_env_path(instance_name)
        env_values = self._load_env_file(env_path)
        env_values[env_key] = api_key.strip()
        env_path.write_text(self._dump_env_file(env_values), encoding="utf-8")
        return env_key

    def _bundle_base_name(self, models_payload: dict) -> str:
        provider_name, _ = self._single_provider_entry(models_payload)
        return provider_name

    def _single_provider_entry(self, models_payload: dict) -> tuple[str, dict]:
        providers = models_payload.get("providers", {})
        if not isinstance(providers, dict) or len(providers) != 1:
            raise ValueError("Collected provider bundles must contain exactly one provider.")
        provider_name = next(iter(providers))
        provider_payload = providers[provider_name]
        if not isinstance(provider_payload, dict):
            raise ValueError(f"Provider '{provider_name}' payload is invalid.")
        return provider_name, provider_payload

    def _store_collected_provider_bundle(
        self,
        *,
        base_name: str,
        auth_payload: dict,
        models_payload: dict,
    ) -> tuple[str, str]:
        if not self.store.provider_exists(base_name):
            self.store.save_provider_bundle(base_name, auth_payload, models_payload)
            return base_name, "saved"

        match_name, match_kind = self._find_matching_provider(base_name, auth_payload, models_payload)
        if match_name:
            if match_kind == "exact":
                return match_name, "skipped"
            merged_auth, merged_models = self._merge_provider_bundles(
                self.store.load_provider_bundle(match_name),
                {"auth_profiles": auth_payload, "models": models_payload},
            )
            self.store.save_provider_bundle(match_name, merged_auth, merged_models)
            return match_name, "merged"

        suffix = 2
        while True:
            candidate = f"{base_name}-{suffix}"
            if not self.store.provider_exists(candidate):
                self.store.save_provider_bundle(candidate, auth_payload, models_payload)
                return candidate, "saved"
            suffix += 1

    def _find_matching_provider(
        self,
        base_name: str,
        auth_payload: dict,
        models_payload: dict,
    ) -> tuple[str | None, str | None]:
        candidate_names = [name for name in self.store.list_provider_names() if name == base_name or name.startswith(f"{base_name}-")]
        incoming_signature = self._provider_signature(auth_payload, models_payload)
        for candidate in candidate_names:
            existing = self.store.load_provider_bundle(candidate)
            if existing["auth_profiles"] == auth_payload and existing["models"] == models_payload:
                return candidate, "exact"
            if self._provider_signature(existing["auth_profiles"], existing["models"]) == incoming_signature:
                return candidate, "merge"
        return None, None

    def _provider_signature(self, auth_payload: dict, models_payload: dict) -> tuple[str, str, str | None, str | None]:
        provider_name, provider_payload = self._single_provider_entry(models_payload)
        api_style = self._infer_api_style(provider_payload)
        endpoint = provider_payload.get("baseUrl")
        if not isinstance(endpoint, str) or not endpoint.strip():
            endpoint = None
        api_key = self._bundle_api_key(auth_payload, models_payload)
        return provider_name, api_style, endpoint, api_key

    def _bundle_api_key(self, auth_payload: dict, models_payload: dict) -> str | None:
        _, provider_payload = self._single_provider_entry(models_payload)
        api_key = provider_payload.get("apiKey")
        if isinstance(api_key, str) and api_key.strip():
            return api_key.strip()
        profiles = auth_payload.get("profiles", {})
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if not isinstance(profile, dict):
                    continue
                for key_name in ("key", "apiKey"):
                    key_value = profile.get(key_name)
                    if isinstance(key_value, str) and key_value.strip():
                        return key_value.strip()
        return None

    def _merge_provider_bundles(self, existing: dict, incoming: dict) -> tuple[dict, dict]:
        merged_auth = copy.deepcopy(existing["auth_profiles"])
        merged_models = copy.deepcopy(existing["models"])

        merged_auth_profiles = merged_auth.setdefault("profiles", {})
        incoming_auth_profiles = incoming["auth_profiles"].get("profiles", {})
        if isinstance(merged_auth_profiles, dict) and isinstance(incoming_auth_profiles, dict):
            merged_auth_profiles.update(copy.deepcopy(incoming_auth_profiles))

        merged_usage = merged_auth.setdefault("usageStats", {})
        incoming_usage = incoming["auth_profiles"].get("usageStats", {})
        if isinstance(merged_usage, dict) and isinstance(incoming_usage, dict):
            merged_usage.update(copy.deepcopy(incoming_usage))

        merged_last_good = merged_auth.setdefault("lastGood", {})
        incoming_last_good = incoming["auth_profiles"].get("lastGood", {})
        if isinstance(merged_last_good, dict) and isinstance(incoming_last_good, dict):
            merged_last_good.update(copy.deepcopy(incoming_last_good))

        if "version" not in merged_auth and "version" in incoming["auth_profiles"]:
            merged_auth["version"] = incoming["auth_profiles"]["version"]

        provider_name, provider_payload = self._single_provider_entry(merged_models)
        _, incoming_provider_payload = self._single_provider_entry(incoming["models"])
        existing_models = provider_payload.get("models", [])
        incoming_models = incoming_provider_payload.get("models", [])
        if not isinstance(existing_models, list):
            existing_models = []
        if not isinstance(incoming_models, list):
            incoming_models = []
        seen_model_ids = {
            entry.get("id")
            for entry in existing_models
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        for model in incoming_models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or model_id in seen_model_ids:
                continue
            existing_models.append(copy.deepcopy(model))
            seen_model_ids.add(model_id)
        provider_payload["models"] = existing_models
        merged_models["providers"] = {provider_name: provider_payload}
        return merged_auth, merged_models

    def _bundle_model_ids(self, models_payload: dict) -> list[str]:
        _, provider_payload = self._single_provider_entry(models_payload)
        models = provider_payload.get("models", [])
        if not isinstance(models, list):
            return []
        model_ids: list[str] = []
        for entry in models:
            if isinstance(entry, dict):
                model_id = entry.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())
        return model_ids

    def _infer_api_style(self, provider_payload: dict) -> str:
        api_name = str(provider_payload.get("api", "") or "").strip().lower()
        if api_name.startswith("anthropic"):
            return "anthropic"
        return "openai"

    def _load_json_file(self, path: Path) -> dict:
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object in '{path}'.")
        return data

    def _configured_provider_names(self, config: dict) -> list[str]:
        providers = config.get("models", {}).get("providers", {})
        if not isinstance(providers, dict):
            return []
        return sorted(name for name, payload in providers.items() if isinstance(name, str) and isinstance(payload, dict))

    def _configured_model_names(self, config: dict) -> list[str]:
        providers = config.get("models", {}).get("providers", {})
        if not isinstance(providers, dict):
            return []
        model_names: list[str] = []
        for provider_name, payload in providers.items():
            if not isinstance(provider_name, str) or not isinstance(payload, dict):
                continue
            models = payload.get("models", [])
            if not isinstance(models, list):
                continue
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_id = model.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_names.append(f"{provider_name}/{model_id.strip()}")
        return sorted(dict.fromkeys(model_names))

    def _configured_agent_models(self, config: dict) -> list[dict[str, str]]:
        agents_config = config.get("agents", {})
        if not isinstance(agents_config, dict):
            return []

        listed_agents = agents_config.get("list", [])
        if isinstance(listed_agents, list) and listed_agents:
            summaries: list[dict[str, str]] = []
            for agent in listed_agents:
                if not isinstance(agent, dict):
                    continue
                agent_name = agent.get("id") or agent.get("name")
                if not isinstance(agent_name, str) or not agent_name.strip():
                    continue
                agent_name = agent_name.strip()
                model_config = agent.get("model", {})
                primary = "-"
                fallbacks = "-"
                if isinstance(model_config, dict):
                    primary_raw = model_config.get("primary")
                    if isinstance(primary_raw, str) and primary_raw.strip():
                        primary = primary_raw.strip()
                    fallbacks_raw = model_config.get("fallbacks", [])
                    if isinstance(fallbacks_raw, list):
                        fallback_list = [item.strip() for item in fallbacks_raw if isinstance(item, str) and item.strip()]
                        if fallback_list:
                            fallbacks = ", ".join(fallback_list)
                summaries.append({"agent": agent_name, "primary": primary, "fallbacks": fallbacks})
            return summaries

        return []

    def _configured_default_agent_model(self, config: dict) -> tuple[str, str]:
        agents_config = config.get("agents", {})
        if not isinstance(agents_config, dict):
            return "-", "-"

        model_config = agents_config.get("defaults", {}).get("model", {})
        if not isinstance(model_config, dict):
            return "-", "-"
        primary = "-"
        primary_raw = model_config.get("primary")
        if isinstance(primary_raw, str) and primary_raw.strip():
            primary = primary_raw.strip()
        fallbacks = "-"
        fallbacks_raw = model_config.get("fallbacks", [])
        if isinstance(fallbacks_raw, list):
            fallback_list = [item.strip() for item in fallbacks_raw if isinstance(item, str) and item.strip()]
            if fallback_list:
                fallbacks = ", ".join(fallback_list)
        return primary, fallbacks

    def _managed_agent_names(self, datadir: Path) -> list[str]:
        agents_dir = datadir / "agents"
        if not agents_dir.exists() or not agents_dir.is_dir():
            return []
        return sorted(path.name for path in agents_dir.iterdir() if path.is_dir())

    def _run_container(self, record: InstanceRecord) -> None:
        env_path = self.store.instance_env_path(record.name)
        self.docker.run_container(record, env_file=env_path if env_path.exists() else None)

    def _configure_gateway(self, record: InstanceRecord) -> None:
        """Write OpenClaw gateway config to datadir before the app reads it."""
        config_path = Path(record.datadir) / "openclaw.json"
        try:
            config: dict = {}
            if config_path.exists():
                raw_config = config_path.read_text(encoding="utf-8").strip()
                if raw_config:
                    config = json.loads(raw_config)
                else:
                    self.reporter(
                        f"Gateway config at {config_path} was empty. Rebuilding a minimal config."
                    )
            gw = config.setdefault("gateway", {})
            gw["bind"] = "lan"
            gw.setdefault("controlUi", {})["allowedOrigins"] = ["*"]
            gw.setdefault("auth", {})["mode"] = record.auth_mode
            config_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.reporter(
                f"Gateway configured: bind=lan, auth.mode={record.auth_mode}, controlUi.allowedOrigins=[*]."
            )
        except Exception as exc:
            self.reporter(f"Could not auto-configure gateway: {exc}")

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

    def _is_port_bind_error(self, exc: CommandError) -> bool:
        details = f"{exc.stderr}\n{exc.stdout}".lower()
        return "port is already allocated" in details or "bind for 0.0.0.0" in details

    def _persist_live_status(self, record: InstanceRecord) -> InstanceRecord:
        live_status = self.docker.container_status(record.container_name)
        changes: dict[str, object] = {"status": live_status}
        if live_status == "running":
            changes["last_error"] = None
        elif record.last_error and live_status in {"missing", "exited", "created", "dead"}:
            changes["status"] = record.status
        updated = updated_record(record, **changes)
        self.store.save_record(updated)
        return updated

    def _latest_transition(self, record: InstanceRecord) -> dict:
        for event in reversed(record.history):
            if event.get("action") in {"upgrade", "rollback"}:
                return event
        raise ValueError(f"Instance '{record.name}' has no rollback history.")

    def _snapshot_summary(self, record: InstanceRecord) -> dict[str, str | None]:
        latest_upgrade: dict | None = None
        latest_rollback: dict | None = None
        for event in reversed(record.history):
            action = event.get("action")
            if latest_upgrade is None and action == "upgrade":
                latest_upgrade = event
            if latest_rollback is None and action == "rollback":
                latest_rollback = event
            if latest_upgrade is not None and latest_rollback is not None:
                break
        return {
            "latest_upgrade_snapshot": latest_upgrade.get("snapshot_dir") if latest_upgrade else None,
            "latest_rollback_snapshot": latest_rollback.get("snapshot_dir") if latest_rollback else None,
            "latest_restored_snapshot": latest_rollback.get("restored_snapshot") if latest_rollback else None,
        }

    def _latest_snapshot_label(self, record: InstanceRecord) -> str:
        for event in reversed(record.history):
            action = event.get("action")
            if action == "rollback":
                target = event.get("to_version") or "-"
                return f"rollback -> {target}"
            if action == "upgrade":
                target = event.get("to_version") or "-"
                return f"upgrade -> {target}"
        return "-"

    def _lifecycle_summary(self, action: str, record: InstanceRecord) -> str:
        verb = {
            "created": "created",
            "retried": "retried",
            "recreated": "recreated",
            "upgraded": "upgraded",
            "rolled_back": "rolled back",
        }.get(action, action)
        if record.status == "running":
            return (
                f"Instance '{record.name}' {verb}. OpenClaw {record.version} is ready on port {record.port}."
            )
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
        return (
            f"Instance '{record.name}' {verb} with status '{record.status}' on port {record.port}."
        )
