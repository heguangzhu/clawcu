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
from typing import Any, Callable

from clawcu import __version__ as clawcu_version
from clawcu.a2a.builder import A2AImageBuilder, a2a_image_tag
from clawcu.core.adapters import ServiceAdapter
from clawcu.core.docker import DockerManager
from clawcu.core.models import AccessInfo, InstanceRecord, InstanceSpec
from clawcu.core.registry import semver_sort_key
from clawcu.core.storage import StateStore
from clawcu.core.subprocess_utils import CommandError, run_command
from clawcu.core.validation import (
    build_instance_record,
    container_name_for_service,
    image_tag_for_service,
    normalize_ref,
    normalize_service_version,
    resolve_datadir,
    updated_record,
    upstream_ref_for_service,
    utc_now_iso,
)
from clawcu.hermes.adapter import HermesAdapter
from clawcu.hermes.manager import (
    DEFAULT_HERMES_IMAGE_REPO,
    HermesManager,
)
from clawcu.openclaw.adapter import OpenClawAdapter
from clawcu.openclaw.manager import (
    DEFAULT_OPENCLAW_IMAGE_REPO,
    DEFAULT_OPENCLAW_IMAGE_REPO_CN,
    OpenClawManager,
)
from clawcu.openclaw.manager import OpenClawManager


class ClawCUService:
    DEFAULT_OPENCLAW_PORT = 18799
    DEFAULT_HERMES_PORT = 8652
    INSTANCE_METADATA_FILENAME = ".clawcu-instance.json"
    PORT_SEARCH_STEP = 10
    PORT_SEARCH_LIMIT = 100
    STARTUP_POLL_INTERVAL_SECONDS = 10.0
    STARTUP_PROGRESS_INTERVAL_SECONDS = 10.0
    STARTUP_TIMEOUT_SECONDS = 120.0
    Reporter = Callable[[str], None]

    def __init__(
        self,
        store: StateStore | None = None,
        docker: DockerManager | None = None,
        openclaw: OpenClawManager | None = None,
        hermes: HermesManager | None = None,
        reporter: Reporter | None = None,
        runner: Callable | None = None,
    ):
        self.store = store or StateStore()
        self.docker = docker or DockerManager()
        self.reporter = reporter or (lambda _message: None)
        self.runner = runner or run_command
        self.openclaw = openclaw or OpenClawManager(self.store, self.docker, reporter=self.reporter)
        self.hermes = hermes or HermesManager(self.store, self.docker, reporter=self.reporter)
        self.adapters: dict[str, ServiceAdapter] = {
            "openclaw": OpenClawAdapter(self.openclaw),
            "hermes": HermesAdapter(self.hermes),
        }
        self.set_reporter(self.reporter)

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)
        if hasattr(self.openclaw, "set_reporter"):
            self.openclaw.set_reporter(self.reporter)
        if hasattr(self.hermes, "set_reporter"):
            self.hermes.set_reporter(self.reporter)

    def adapter_for_service(self, service: str) -> ServiceAdapter:
        try:
            return self.adapters[service]
        except KeyError as exc:
            raise ValueError(f"Unsupported service '{service}'.") from exc

    def adapter_for_record(self, record: InstanceRecord) -> ServiceAdapter:
        return self.adapter_for_service(record.service)

    def _effective_auth_mode(self, record: InstanceRecord) -> str:
        if record.service == "openclaw":
            return "token"
        return record.auth_mode

    def _normalize_requested_image(self, image: str | None) -> str | None:
        if image is None:
            return None
        cleaned = image.strip()
        if not cleaned:
            raise ValueError("Image cannot be empty.")
        return cleaned

    def _planned_runtime_image(
        self,
        record: InstanceRecord,
        *,
        version: str,
        image: str | None = None,
    ) -> str:
        explicit_image = self._normalize_requested_image(image)
        if explicit_image is not None:
            return explicit_image
        manager = self._service_manager(record)
        if hasattr(manager, "official_image_tag"):
            return manager.official_image_tag(version)
        return f"{record.service}:{version}"

    def _make_runtime_tree_writable(self, root: Path) -> None:
        if not root.exists():
            return
        for path in [root, *root.rglob("*")]:
            try:
                if path.is_symlink():
                    continue
                if path.is_dir():
                    path.chmod(0o777)
                elif path.is_file():
                    path.chmod(0o666)
            except OSError:
                continue

    def pull_service(self, service_name: str, version: str) -> str:
        adapter = self.adapter_for_service(service_name)
        normalized = normalize_service_version(service_name, version)
        self.reporter(
            f"Starting {adapter.display_name} artifact preparation for version {normalized}."
        )
        self.store.append_log(f"pull {service_name} version={normalized}")
        image_tag = adapter.prepare_artifact(normalized)
        self.store.append_log(f"prepared image {image_tag}")
        self.reporter(f"Finished preparing Docker image {image_tag}.")
        return image_tag

    def pull_openclaw(self, version: str) -> str:
        return self.pull_service("openclaw", version)

    def pull_hermes(self, version: str) -> str:
        return self.pull_service("hermes", version)

    def collect_providers(
        self,
        *,
        all_instances: bool = False,
        instance: str | None = None,
        path: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, list[str]]:
        selected = [bool(all_instances), instance is not None, path is not None]
        if sum(selected) != 1:
            raise ValueError("Choose exactly one source: --all, --instance, or --path.")

        roots: list[tuple[str, str, Path, dict[str, str]]] = []
        if all_instances:
            records = self.store.list_records()
            for record in records:
                adapter = self.adapter_for_record(record)
                roots.append(
                    (
                        record.service,
                        f"instance:{record.name}",
                        Path(record.datadir),
                        self._load_env_file(adapter.env_path(self, record)),
                    )
                )
            local_root = self._local_openclaw_home()
            if local_root.exists():
                roots.append(("openclaw", f"path:{local_root}", local_root, self._load_env_file(local_root / ".env")))
            local_hermes_root = self._local_hermes_home()
            if local_hermes_root.exists():
                roots.append(("hermes", f"path:{local_hermes_root}", local_hermes_root, self._load_env_file(local_hermes_root / ".env")))
        elif instance is not None:
            record = self.store.load_record(instance)
            adapter = self.adapter_for_record(record)
            roots.append(
                (
                    record.service,
                    f"instance:{record.name}",
                    Path(record.datadir),
                    self._load_env_file(adapter.env_path(self, record)),
                )
            )
        else:
            resolved_path = Path(path or "").expanduser().resolve()
            managed_record = next(
                (
                    record
                    for record in self.store.list_records()
                    if Path(record.datadir).expanduser().resolve() == resolved_path
                ),
                None,
            )
            if managed_record is not None:
                adapter = self.adapter_for_record(managed_record)
                service_name = managed_record.service
                env_values = self._load_env_file(adapter.env_path(self, managed_record))
            else:
                service_name = "hermes" if (resolved_path / "config.yaml").exists() else "openclaw"
                env_values = self._load_env_file(resolved_path / ".env")
            roots.append((service_name, f"path:{path}", resolved_path, env_values))

        saved: list[str] = []
        merged: list[str] = []
        overwritten: list[str] = []
        skipped: list[str] = []
        scanned: list[str] = []

        for service_name, source_label, root, env_values in roots:
            scanned.append(str(root))
            try:
                bundles = self.adapter_for_service(service_name).scan_model_config_bundles(self, root, env_values)
            except FileNotFoundError:
                if all_instances:
                    continue
                raise
            for bundle in bundles:
                target_name, status = self._store_collected_provider_bundle(
                    bundle, overwrite=overwrite
                )
                collection_label = f"{target_name} ({source_label})"
                if status == "saved":
                    saved.append(collection_label)
                elif status == "merged":
                    merged.append(collection_label)
                elif status == "overwritten":
                    overwritten.append(collection_label)
                else:
                    skipped.append(collection_label)

        self.store.append_log(
            "provider collect "
            f"sources={','.join(scanned)} "
            f"saved={','.join(saved)} "
            f"merged={','.join(merged)} "
            f"overwritten={','.join(overwritten)} "
            f"skipped={','.join(skipped)}"
        )
        return {
            "saved": saved,
            "merged": merged,
            "overwritten": overwritten,
            "skipped": skipped,
            "scanned": scanned,
        }

    def list_providers(self) -> list[dict]:
        providers: list[dict] = []
        for service_name, name in self.store.list_provider_refs():
            bundle = self.store.load_provider_bundle(service_name, name)
            metadata = bundle.get("metadata", {})
            endpoint = metadata.get("endpoint") if isinstance(metadata, dict) else None
            providers.append(
                {
                    "service": service_name,
                    "name": name,
                    "provider": metadata.get("provider") if isinstance(metadata, dict) else name,
                    "api_style": metadata.get("api_style") if isinstance(metadata, dict) else "openai",
                    "api_key": self._provider_bundle_api_key(bundle),
                    "api_key_state": self._provider_bundle_api_key_state(bundle),
                    "endpoint": endpoint if isinstance(endpoint, str) else None,
                    "models": self.adapter_for_service(service_name).provider_models(self, bundle),
                }
            )
        return providers

    def _resolve_provider_ref(self, name: str, *, target_service: str | None = None) -> tuple[str, str]:
        if ":" in name:
            service_name, provider_name = name.split(":", 1)
            self.store.load_provider_bundle(service_name, provider_name)
            return service_name, provider_name
        refs = self.store.list_provider_refs()
        if target_service is not None:
            for service_name, provider_name in refs:
                if service_name == target_service and provider_name == name:
                    return service_name, provider_name
        matches = [(service_name, provider_name) for service_name, provider_name in refs if provider_name == name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise FileNotFoundError(f"Provider '{name}' was not found.")
        raise ValueError(
            f"Provider name '{name}' is ambiguous. Use an explicit '<service>:<name>' reference."
        )

    def show_provider(self, name: str) -> dict:
        service_name, provider_name = self._resolve_provider_ref(name)
        bundle = self.store.load_provider_bundle(service_name, provider_name)
        return bundle

    def find_instances_using_provider(self, name: str) -> list[dict[str, str]]:
        """Return managed instance/agent pairs referencing this provider.

        Scans the agent-level provider summary for every managed instance
        and returns an entry whenever the provider's ``providers`` column
        lists the requested name. Used by ``provider remove`` to warn
        before deleting a bundle that some instance is still pointing at.
        """
        service_name, provider_name = self._resolve_provider_ref(name)
        hits: list[dict[str, str]] = []
        for row in self.list_agent_summaries():
            if row.get("service") and service_name != "any" and row.get("service") != service_name:
                continue
            providers_field = str(row.get("providers") or "")
            for candidate in self._split_summary_values(providers_field):
                if candidate == provider_name:
                    hits.append(
                        {
                            "instance": str(row.get("instance") or ""),
                            "agent": str(row.get("agent") or ""),
                            "service": str(row.get("service") or ""),
                        }
                    )
                    break
        return hits

    def remove_provider(self, name: str, *, force: bool = False) -> list[dict[str, str]]:
        """Delete a collected provider bundle.

        Returns the list of instances currently referencing the provider
        (empty when safe). When ``force=False`` and at least one
        reference is found, raises ``ValueError`` instead of deleting —
        the CLI layer catches this to surface the warning; callers that
        have already confirmed pass ``force=True``.
        """
        service_name, provider_name = self._resolve_provider_ref(name)
        self.store.load_provider_bundle(service_name, provider_name)
        in_use = self.find_instances_using_provider(name)
        if in_use and not force:
            refs = ", ".join(f"{row['instance']}/{row['agent']}" for row in in_use)
            raise ValueError(
                f"Provider '{provider_name}' is in use by: {refs}. "
                "Re-run with --force to remove anyway."
            )
        self.store.delete_provider(service_name, provider_name)
        self.store.append_log(
            f"provider remove service={service_name} name={provider_name} force={force} in_use={len(in_use)}"
        )
        return in_use

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
        service_name, provider_name = self._resolve_provider_ref(provider, target_service=record.service)
        bundle = self.store.load_provider_bundle(service_name, provider_name)
        adapter = self.adapter_for_record(record)
        return adapter.apply_provider(
            self,
            bundle,
            record.name,
            agent=agent,
            primary=primary,
            fallbacks=fallbacks,
            persist=persist,
        )

    def plan_apply_provider(
        self,
        provider: str,
        instance: str,
        agent: str = "main",
        *,
        primary: str | None = None,
        fallbacks: list[str] | None = None,
        persist: bool = False,
    ) -> dict[str, object]:
        """Compute an apply_provider plan without touching disk.

        Returns the provider/instance/agent, the runtime dir the adapter
        would write under, the file list that would be rewritten, and the
        projected env key + env file path when ``persist`` is requested.
        Pure read — no files are modified.
        """
        record = self.store.load_record(instance)
        service_name, provider_name = self._resolve_provider_ref(
            provider, target_service=record.service
        )
        bundle = self.store.load_provider_bundle(service_name, provider_name)
        adapter = self.adapter_for_record(record)
        agent_name = (agent or "main").strip() or "main"

        if service_name == "openclaw":
            runtime_dir = Path(record.datadir) / "agents" / agent_name / "agent"
            writes = [
                str(runtime_dir / "auth-profiles.json"),
                str(runtime_dir / "models.json"),
                str(Path(record.datadir) / "openclaw.json"),
            ]
        else:
            # Hermes writes per-agent profile files under the instance home.
            runtime_dir = Path(record.datadir)
            writes = [str(runtime_dir / f"profiles-{agent_name}.yaml")]

        env_key = None
        env_path = None
        if persist:
            try:
                api_key = self._provider_bundle_api_key(bundle)
                if isinstance(api_key, str) and api_key.strip():
                    env_key = self._provider_env_key(provider_name)
            except Exception:
                env_key = None
            try:
                env_path = str(adapter.env_path(self, record))
            except Exception:
                env_path = None

        return {
            "provider": provider_name,
            "service": service_name,
            "instance": record.name,
            "agent": agent_name,
            "runtime_dir": str(runtime_dir),
            "writes": writes,
            "persist": bool(persist),
            "env_key": env_key or "-",
            "env_path": env_path or "-",
            "primary": primary or "-",
            "fallbacks": ", ".join(fallbacks) if fallbacks else "-",
        }

    def list_provider_models(self, name: str) -> list[str]:
        service_name, provider_name = self._resolve_provider_ref(name)
        bundle = self.store.load_provider_bundle(service_name, provider_name)
        return self.adapter_for_service(service_name).provider_models(self, bundle)

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
        checks.append(
            {
                "name": "hermes_image_repo",
                "status": "ok",
                "ok": True,
                "summary": (
                    "Hermes image repo is configured as "
                    f"{self.get_hermes_image_repo()}."
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
        self.store.set_bootstrap_home(resolved)
        self.store.switch_home(resolved)
        self.openclaw.store = self.store
        self.openclaw.image_repo = self.store.get_openclaw_image_repo() or os.environ.get(
            "CLAWCU_OPENCLAW_IMAGE_REPO",
            getattr(self.openclaw, "image_repo", "ghcr.io/openclaw/openclaw"),
        )
        self.hermes.image_repo = self.store.get_hermes_image_repo() or os.environ.get(
            "CLAWCU_HERMES_IMAGE_REPO",
            getattr(self.hermes, "image_repo", DEFAULT_HERMES_IMAGE_REPO),
        )
        self.store.append_log(f"setup clawcu_home={resolved}")
        return resolved

    def get_openclaw_image_repo(self) -> str:
        return self.store.get_openclaw_image_repo() or getattr(
            self.openclaw,
            "image_repo",
            DEFAULT_OPENCLAW_IMAGE_REPO,
        )

    def get_hermes_image_repo(self) -> str:
        return self.store.get_hermes_image_repo() or getattr(
            self.hermes,
            "image_repo",
            DEFAULT_HERMES_IMAGE_REPO,
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

    def set_hermes_image_repo(self, image_repo: str) -> str:
        cleaned = image_repo.strip()
        if not cleaned:
            raise ValueError("Hermes image repo cannot be empty.")
        self.store.set_hermes_image_repo(cleaned)
        self.hermes.image_repo = cleaned
        self.store.append_log(f"setup hermes_image_repo={cleaned}")
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

    def create_service(
        self,
        service_name: str,
        *,
        name: str,
        version: str,
        image: str | None = None,
        datadir: str | None = None,
        port: int | None = None,
        cpu: str,
        memory: str,
        a2a: bool = False,
        a2a_hop_budget: int | None = None,
        a2a_advertise_host: str | None = None,
    ) -> InstanceRecord:
        adapter = self.adapter_for_service(service_name)
        auto_port = port is None
        if a2a_hop_budget is not None:
            if not a2a:
                raise ValueError("a2a_hop_budget requires a2a=True.")
            if not isinstance(a2a_hop_budget, int) or isinstance(a2a_hop_budget, bool) or a2a_hop_budget < 1:
                raise ValueError("a2a_hop_budget must be a positive integer (>= 1).")
        if a2a_advertise_host is not None:
            if not a2a:
                raise ValueError("a2a_advertise_host requires a2a=True.")
            a2a_advertise_host = str(a2a_advertise_host).strip() or None
        self.reporter("Step 1/5: Validating options and resolving defaults. This should take a second or two.")
        spec = adapter.build_spec(
            self,
            name=name,
            version=version,
            datadir=datadir,
            port=port,
            cpu=cpu,
            memory=memory,
        )
        spec = replace(spec, a2a_enabled=bool(a2a), a2a_advertise_host=a2a_advertise_host)
        if self.store.instance_path(spec.name).exists():
            raise ValueError(f"Instance '{spec.name}' already exists.")
        container_name = container_name_for_service(spec.service, spec.name)
        if self.docker.container_status(container_name) != "missing":
            raise ValueError(
                f"Instance '{spec.name}' already exists. Docker container '{container_name}' is already present."
            )

        a2a_note = " a2a=on" if spec.a2a_enabled else ""
        self.reporter(
            f"Resolved instance settings: datadir={spec.datadir}, port={spec.port}, cpu={spec.cpu}, memory={spec.memory}, auth={spec.auth_mode}.{a2a_note}"
        )
        explicit_image = self._normalize_requested_image(image)
        self.store.append_log(
            f"create instance service={spec.service} name={spec.name} version={spec.version} datadir={spec.datadir} a2a={spec.a2a_enabled}"
            + (f" image={explicit_image}" if explicit_image else "")
        )
        if explicit_image is not None:
            prepared_image = explicit_image
            self.reporter(
                f"Step 2/5: Using explicit runtime image {prepared_image} for this instance. "
                "Docker will pull it on container start if it is missing locally."
            )
        else:
            prepared_image = adapter.prepare_artifact(spec.version)
        if spec.a2a_enabled:
            prepared_image = self._bake_a2a_image(spec.service, spec.version, prepared_image)
        spec = replace(spec, image_tag_override=prepared_image)
        datadir_path = Path(spec.datadir)
        self.reporter("Step 4/5: Preparing the local data directory and runtime metadata. This usually takes a few seconds.")
        datadir_path.mkdir(parents=True, exist_ok=True)
        history = [
            {
                "action": "create_requested",
                "timestamp": utc_now_iso(),
                "version": spec.version,
                "image_tag": prepared_image,
                "clawcu_version": clawcu_version,
                "auth_mode": spec.auth_mode,
                "service": spec.service,
            }
        ]
        env_overrides: dict[str, str] = {}
        if a2a and a2a_hop_budget is not None:
            env_overrides["A2A_HOP_BUDGET"] = str(a2a_hop_budget)
        self.reporter("Step 5/5: Starting the Docker container and checking health. This usually takes a few seconds.")
        live_record = self._start_new_instance(
            spec,
            history=history,
            auto_port=auto_port,
            env_overrides=env_overrides or None,
        )
        self.reporter(self._lifecycle_summary("created", live_record))
        return live_record

    def create_openclaw(
        self,
        *,
        name: str,
        version: str,
        image: str | None = None,
        datadir: str | None = None,
        port: int | None = None,
        cpu: str,
        memory: str,
        a2a: bool = False,
        a2a_hop_budget: int | None = None,
        a2a_advertise_host: str | None = None,
    ) -> InstanceRecord:
        return self.create_service(
            "openclaw",
            name=name,
            version=version,
            image=image,
            datadir=datadir,
            port=port,
            cpu=cpu,
            memory=memory,
            a2a=a2a,
            a2a_hop_budget=a2a_hop_budget,
            a2a_advertise_host=a2a_advertise_host,
        )

    def create_hermes(
        self,
        *,
        name: str,
        version: str,
        image: str | None = None,
        datadir: str | None = None,
        port: int | None = None,
        cpu: str,
        memory: str,
        a2a: bool = False,
        a2a_hop_budget: int | None = None,
        a2a_advertise_host: str | None = None,
    ) -> InstanceRecord:
        return self.create_service(
            "hermes",
            name=name,
            version=version,
            image=image,
            datadir=datadir,
            port=port,
            cpu=cpu,
            memory=memory,
            a2a=a2a,
            a2a_hop_budget=a2a_hop_budget,
            a2a_advertise_host=a2a_advertise_host,
        )

    def _bake_a2a_image(self, service: str, base_version: str, base_image: str) -> str:
        """Build (or reuse) the a2a variant of ``base_image`` for ``service``."""
        builder = A2AImageBuilder(
            docker=self.docker,
            clawcu_version=clawcu_version,
            reporter=self.reporter,
        )
        return builder.ensure_image(service, base_version, base_image)

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
            adapter = self.adapter_for_record(record)
            access = adapter.access_info(self, record)
            payload.update(adapter.instance_provider_summary(self, record))
            payload["port"] = adapter.display_port(self, record)
            payload["source"] = "managed"
            payload["home"] = record.datadir
            payload["snapshot"] = self._latest_snapshot_label(record)
            payload["access_url"] = access.base_url or "-"
            payload["auth_hint"] = access.auth_hint or "-"
            summaries.append(payload)
        return summaries

    def list_agent_summaries(self, *, running_only: bool = False) -> list[dict]:
        summaries: list[dict] = []
        for record in self.list_instances(running_only=running_only):
            adapter = self.adapter_for_record(record)
            for agent_summary in adapter.instance_agent_summaries(self, record):
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
        summaries: list[dict] = []
        for adapter in self.adapters.values():
            summaries.extend(adapter.local_instance_summaries(self))
        return summaries

    def list_removed_instance_summaries(self) -> list[dict]:
        summaries: list[dict] = []
        for child in self._iter_removed_instance_roots():
            for adapter in self.adapters.values():
                summary = adapter.removed_instance_summary(self, child)
                if summary is not None:
                    summaries.append(summary)
                    break
        return summaries

    def _iter_removed_instance_roots(self):
        internal_dirs = {
            self.store.paths.instances_dir.resolve(),
            self.store.paths.providers_dir.resolve(),
            self.store.paths.sources_dir.resolve(),
            self.store.paths.logs_dir.resolve(),
            self.store.paths.snapshots_dir.resolve(),
        }
        managed_datadirs = {
            Path(record.datadir).expanduser().resolve(strict=False)
            for record in self.store.list_records()
        }
        for child in sorted(self.store.paths.home.iterdir(), key=lambda path: path.name):
            if not child.is_dir():
                continue
            resolved_child = child.resolve()
            if resolved_child in internal_dirs or resolved_child in managed_datadirs:
                continue
            if child.name.startswith("."):
                continue
            yield child

    def _find_removed_instance_root(self, name: str) -> Path | None:
        for child in self._iter_removed_instance_roots():
            if child.name == name:
                return child
        return None

    def _instance_metadata_path(self, datadir: Path) -> Path:
        return datadir / self.INSTANCE_METADATA_FILENAME

    def _load_instance_metadata(self, datadir: Path) -> dict:
        path = self._instance_metadata_path(datadir)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_instance_metadata(self, record: InstanceRecord) -> None:
        path = self._instance_metadata_path(Path(record.datadir))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "service": record.service,
            "name": record.name,
            "version": record.version,
            "image_tag": record.image_tag,
            "datadir": record.datadir,
            "port": record.port,
            "dashboard_port": record.dashboard_port,
            "cpu": record.cpu,
            "memory": record.memory,
            "auth_mode": record.auth_mode,
            "updated_at": record.updated_at,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _coerce_metadata_port(self, value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
            return parsed if parsed > 0 else None
        return None

    def _build_removed_instance_spec(self, name: str, *, version: str | None = None) -> InstanceSpec:
        root = self._find_removed_instance_root(name)
        if root is None:
            raise FileNotFoundError(f"Instance '{name}' was not found.")
        metadata = self._load_instance_metadata(root)
        hinted_service = metadata.get("service")
        if isinstance(hinted_service, str) and hinted_service in self.adapters:
            adapter = self.adapters[hinted_service]
            spec = adapter.removed_instance_spec(self, root, version=version)
            if spec is not None:
                return spec
        for adapter in self.adapters.values():
            spec = adapter.removed_instance_spec(self, root, version=version)
            if spec is not None:
                return spec
        raise FileNotFoundError(f"Instance '{name}' was not found.")

    def list_local_agent_summaries(self) -> list[dict]:
        summaries: list[dict] = []
        for adapter in self.adapters.values():
            summaries.extend(adapter.local_agent_summaries(self))
        return summaries

    def inspect_instance(self, name: str) -> dict:
        record = self._persist_live_status(self.store.load_record(name))
        inspection = self.docker.inspect_container(record.container_name)
        access = self.adapter_for_record(record).access_info(self, record)
        return {
            "instance": record.to_dict(),
            "snapshots": self._snapshot_summary(record),
            "access": {
                "base_url": access.base_url,
                "readiness_label": access.readiness_label,
                "auth_hint": access.auth_hint,
                "token": access.token,
            },
            "container": inspection,
            "a2a": self._a2a_inspect_section(record),
        }

    def _a2a_inspect_section(self, record: InstanceRecord) -> dict[str, Any] | None:
        """Review-2 P1-F (iter 3): surface A2A wiring on inspect.

        When A2A is enabled, read the instance env file for the user-
        visible A2A knobs (hop budget, registry URL) and compute the
        bridge port + MCP URL. Operators can see the whole A2A config
        without ``clawcu getenv | grep``. Returns ``None`` for stock
        instances so the renderer can skip the whole section.
        """
        if not getattr(record, "a2a_enabled", False):
            return None
        from clawcu.a2a.card import bridge_port_for

        env: dict[str, str] = {}
        try:
            env_path = self.adapter_for_record(record).env_path(self, record)
            if env_path.exists():
                env = self._load_env_file(env_path)
        except Exception:
            # Env file read is best-effort; a missing/unreadable file
            # shouldn't break `inspect`. The adapter layer already
            # guarantees env_path exists for A2A instances.
            env = {}

        bridge_port = bridge_port_for(record)
        hop_budget_raw = env.get("A2A_HOP_BUDGET", "").strip()
        try:
            hop_budget: int | None = int(hop_budget_raw) if hop_budget_raw else None
        except ValueError:
            hop_budget = None

        registry_url = (
            env.get("A2A_REGISTRY_URL", "").strip()
            or "http://host.docker.internal:9100"
        )
        return {
            "enabled": True,
            "port": bridge_port,
            "registry_url": registry_url,
            "hop_budget": hop_budget,
            "hop_budget_default": 8,
            "mcp_url": f"http://127.0.0.1:{bridge_port}/mcp",
        }

    def dashboard_url(self, name: str) -> str:
        record = self._persist_live_status(self.store.load_record(name))
        access = self.adapter_for_record(record).access_info(self, record)
        if access.base_url:
            return access.base_url
        raise ValueError(f"Instance '{record.name}' does not expose a dashboard URL.")

    def token(self, name: str) -> str:
        record = self._persist_live_status(self.store.load_record(name))
        return self.adapter_for_record(record).token(self, name)

    def set_instance_env(self, name: str, assignments: list[str]) -> dict[str, object]:
        if not assignments:
            raise ValueError("Please provide at least one KEY=VALUE assignment.")

        record = self.store.load_record(name)
        env_path = self.adapter_for_record(record).env_path(self, record)
        env_path.parent.mkdir(parents=True, exist_ok=True)
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
        env_path = self.adapter_for_record(record).env_path(self, record)
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
        env_path = self.adapter_for_record(record).env_path(self, record)
        env_path.parent.mkdir(parents=True, exist_ok=True)
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

    def set_hermes_identity(self, name: str, source: Path | str) -> dict[str, object]:
        """Install a user-provided SOUL.md as the hermes instance's persona.

        The file is copied to ``<datadir>/SOUL.md`` — the same location the
        ``configure_before_run`` scaffolder uses. Because the container mounts
        ``HERMES_HOME=/opt/data`` → datadir, ``prompt_builder.load_soul_md``
        picks the new persona up on the next chat turn without restarting.
        """
        from clawcu.hermes.adapter import HERMES_SOUL_FILENAME

        record = self.store.load_record(name)
        if record.service != "hermes":
            raise ValueError(
                f"Instance '{record.name}' is a {record.service} service; "
                "`identity set` is only available for hermes instances."
            )
        src = Path(source).expanduser()
        if not src.is_file():
            raise ValueError(f"Identity file not found: {src}")
        content = src.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(
                f"Identity file {src} is empty — refusing to overwrite "
                "the existing SOUL.md with a blank persona."
            )
        datadir = Path(record.datadir)
        datadir.mkdir(parents=True, exist_ok=True)
        target = datadir / HERMES_SOUL_FILENAME
        target.write_text(content, encoding="utf-8")
        self.store.append_log(
            f"identity_set instance={record.name} source={src} target={target} bytes={len(content)}"
        )
        return {
            "instance": record.name,
            "source": str(src),
            "target": str(target),
            "bytes": len(content),
            "status": record.status,
        }

    def _config_provider_summary(self, config: dict) -> dict[str, str]:
        providers = self._configured_provider_names(config)
        models = self._configured_model_names(config)
        return {
            "providers": ", ".join(providers) if providers else "-",
            "models": ", ".join(models) if models else "-",
        }

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

    def _apply_env_overrides(
        self,
        adapter,
        record: InstanceRecord,
        overrides: dict[str, str],
    ) -> None:
        """Merge ``overrides`` into the instance env file (create-time only).

        Runs *after* adapter.configure_before_run so that Hermes' persona
        scaffolder can seed ``API_SERVER_KEY`` first; the overrides merge
        on top without clobbering adapter-managed keys we don't touch.
        """
        env_path = adapter.env_path(self, record)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_values = self._load_env_file(env_path)
        env_values.update(overrides)
        env_path.write_text(self._dump_env_file(env_values), encoding="utf-8")

    def _load_env_text(self, text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            values[key] = value
        return values

    def _config_version(self, config: dict) -> str:
        version = config.get("meta", {}).get("lastTouchedVersion")
        if isinstance(version, str) and version.strip():
            return version.strip()
        return "-"

    def _local_openclaw_home(self) -> Path:
        return Path.home() / ".openclaw"

    def _local_hermes_home(self) -> Path:
        return Path.home() / ".hermes"

    def approve_pairing(self, name: str, request_id: str | None = None) -> str:
        record = self._persist_live_status(self.store.load_record(name))
        return self.adapter_for_record(record).approve_pairing(self, name, request_id=request_id)

    def list_pending_pairings(self, name: str) -> list[dict[str, object]]:
        record = self._persist_live_status(self.store.load_record(name))
        return self.adapter_for_record(record).list_pending_pairings(self, name)

    def list_agents(self, name: str) -> list[str]:
        record = self.store.load_record(name)
        return self.adapter_for_record(record).list_agents(self, record)

    def configure_instance(self, name: str, extra_args: list[str] | None = None) -> None:
        record = self._persist_live_status(self.store.load_record(name))
        self.adapter_for_record(record).configure_instance(self, name, extra_args=extra_args)

    def exec_instance(
        self,
        name: str,
        command: list[str],
        *,
        workdir: str | None = None,
        user: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("Please provide a command to run inside the instance.")
        record = self._persist_live_status(self.store.load_record(name))
        adapter = self.adapter_for_record(record)
        env_values = adapter.exec_env(self, record)
        # User-supplied --env values win over the adapter's computed env.
        if extra_env:
            env_values.update(extra_env)
        command = adapter.normalize_exec_command(self, record, command)
        self.store.append_log(
            f"exec instance name={record.name} command={' '.join(command)} "
            f"workdir={workdir or '-'} user={user or '-'}"
        )
        extra_kwargs: dict[str, object] = {"env": env_values}
        if workdir is not None:
            extra_kwargs["workdir"] = workdir
        if user is not None:
            extra_kwargs["user"] = user
        self.docker.exec_in_container_interactive(
            record.container_name,
            command,
            **extra_kwargs,
        )

    def tui_instance(self, name: str, *, agent: str = "main") -> None:
        record = self._persist_live_status(self.store.load_record(name))
        self.adapter_for_record(record).tui_instance(self, name, agent=agent)

    def start_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        adapter = self.adapter_for_record(record)
        self.reporter(
            f"Step 1/3: Loading the saved {adapter.display_name} instance record for '{record.name}'."
        )
        inspection = self.docker.inspect_container(record.container_name)
        if inspection is None:
            self.reporter(
                f"Instance '{record.name}' has no Docker container right now. Recreating it from the saved record."
            )
            return self.recreate_instance(name, prepare_artifact=False)
        if not adapter.container_env_matches(self, record, inspection):
            self.reporter(
                f"Instance '{record.name}' needs a container refresh to pick up the current environment file. Recreating it instead of using docker start."
            )
            return self.recreate_instance(name, prepare_artifact=False)
        self.reporter(
            f"Step 2/3: Starting Docker container {record.container_name}. This usually takes a few seconds."
        )
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
        self.reporter("Step 3/3: Refreshing live status after Docker start.")
        started = self._persist_live_status(record)
        access = adapter.access_info(self, started)
        if started.status in {"starting", "created"}:
            self.reporter(
                f"{adapter.display_name} is still {started.status} on port {started.port}. "
                f"Check 'clawcu inspect {started.name}' or 'clawcu logs {started.name}' if it takes too long."
            )
        elif access.base_url:
            self.reporter(
                f"{adapter.display_name} reported status '{started.status}'. Access URL: {access.base_url}"
            )
        else:
            self.reporter(
                f"{adapter.display_name} reported status '{started.status}' after Docker start."
            )
        return started

    def stop_instance(self, name: str, *, timeout: int | None = None) -> InstanceRecord:
        record = self.store.load_record(name)
        self.docker.stop_container(record.container_name, timeout=timeout)
        suffix = f" timeout={timeout}" if timeout is not None else ""
        self.store.append_log(f"stop instance name={record.name}{suffix}")
        return self._persist_live_status(record)

    def restart_instance(
        self,
        name: str,
        *,
        recreate_if_config_changed: bool = True,
    ) -> InstanceRecord:
        record = self.store.load_record(name)
        if recreate_if_config_changed:
            adapter = self.adapter_for_record(record)
            inspection = self.docker.inspect_container(record.container_name)
            if inspection is None:
                self.reporter(
                    f"Instance '{record.name}' has no live container; promoting restart to recreate."
                )
                return self.recreate_instance(name, prepare_artifact=False)
            if not adapter.container_env_matches(self, record, inspection):
                self.reporter(
                    f"Detected env drift for instance '{record.name}'; promoting restart to recreate so the new env file takes effect."
                )
                return self.recreate_instance(name, prepare_artifact=False)
        self.docker.restart_container(record.container_name)
        self.store.append_log(f"restart instance name={record.name}")
        return self._persist_live_status(record)

    def retry_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        if record.status != "create_failed":
            raise ValueError(
                f"Instance '{name}' is in status '{record.status}'. Only create_failed instances can be retried."
            )
        effective_auth_mode = self._effective_auth_mode(record)

        self.reporter("Step 1/4: Loading the failed instance record and validating retry state.")
        self.reporter(
            f"Retrying instance '{record.name}' with version {record.version}, datadir={record.datadir}, port={record.port}, cpu={record.cpu}, memory={record.memory}."
        )
        self.store.append_log(f"retry instance name={record.name} version={record.version}")
        prepared_image = record.image_tag
        self.reporter(
            f"Step 2/4: Reusing saved runtime image {prepared_image}. "
            "Docker will pull it on container start if it is missing locally."
        )
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
            auth_mode=effective_auth_mode,
            dashboard_port=record.dashboard_port,
            image_tag_override=prepared_image,
            a2a_enabled=record.a2a_enabled,
        )
        history = copy.deepcopy(record.history)
        history.append(
            {
                "action": "retry_requested",
                "timestamp": utc_now_iso(),
                "version": record.version,
                "from_status": record.status,
                "a2a_enabled": record.a2a_enabled,
            }
        )
        live_record = self._start_new_instance(spec, history=history, auto_port=True)
        self.reporter(self._lifecycle_summary("retried", live_record))
        return live_record

    def recreate_instance(
        self,
        name: str,
        *,
        prepare_artifact: bool = True,
        fresh: bool = False,
        timeout: int | None = None,
        version: str | None = None,
        a2a: bool | None = None,
    ) -> InstanceRecord:
        try:
            record = self.store.load_record(name)
        except FileNotFoundError:
            return self._recreate_removed_instance(
                name,
                prepare_artifact=prepare_artifact,
                fresh=fresh,
                timeout=timeout,
                version=version,
                a2a=a2a,
            )
        if version is not None:
            raise ValueError(
                f"Instance '{name}' already exists. Use `clawcu upgrade {name} --version {version}` to change versions."
            )
        effective_auth_mode = self._effective_auth_mode(record)
        # If the caller didn't explicitly toggle a2a, preserve the record's
        # flavor. A change in flavor always re-runs artifact preparation,
        # so the ``prepare_artifact=False`` shortcut only applies when the
        # flavor hasn't moved.
        effective_a2a = record.a2a_enabled if a2a is None else bool(a2a)
        if effective_a2a != record.a2a_enabled:
            prepare_artifact = True
        a2a_note = " a2a=on" if effective_a2a else " a2a=off"
        self.reporter(
            f"Recreating instance '{record.name}' (service={record.service}, version {record.version}, port {record.port}, auth={effective_auth_mode}){a2a_note}."
        )
        self.store.append_log(
            f"recreate instance name={record.name} version={record.version} fresh={fresh} timeout={timeout} a2a={effective_a2a}"
        )
        if effective_a2a != record.a2a_enabled:
            adapter = self.adapter_for_record(record)
            prepared_image = adapter.prepare_artifact(record.version)
            if effective_a2a:
                prepared_image = self._bake_a2a_image(record.service, record.version, prepared_image)
        else:
            prepared_image = record.image_tag
            self.reporter(
                f"Reusing the saved runtime image {prepared_image} for recreate. "
                "Docker will pull it on container start if it is missing locally."
            )
        if timeout is not None:
            try:
                self.docker.stop_container(record.container_name, timeout=timeout)
            except Exception as exc:
                self.reporter(
                    f"Graceful stop failed during recreate (proceeding to force-remove): {exc}"
                )
        self.docker.remove_container(record.container_name, missing_ok=True)
        if fresh:
            self._wipe_datadir(record)

        spec = InstanceSpec(
            service=record.service,
            name=record.name,
            version=record.version,
            datadir=record.datadir,
            port=record.port,
            cpu=record.cpu,
            memory=record.memory,
            auth_mode=effective_auth_mode,
            dashboard_port=record.dashboard_port,
            image_tag_override=prepared_image,
            a2a_enabled=effective_a2a,
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
                "a2a_enabled": effective_a2a,
            }
        )
        live_record = self._start_new_instance(spec, history=history, auto_port=False)
        self.reporter(self._lifecycle_summary("recreated", live_record))
        return live_record

    def _recreate_removed_instance(
        self,
        name: str,
        *,
        prepare_artifact: bool,
        fresh: bool,
        timeout: int | None,
        version: str | None,
        a2a: bool | None = None,
    ) -> InstanceRecord:
        spec = self._build_removed_instance_spec(name, version=version)
        effective_a2a = bool(a2a) if a2a is not None else getattr(spec, "a2a_enabled", False)
        spec = replace(spec, a2a_enabled=effective_a2a)
        adapter = self.adapter_for_service(spec.service)
        a2a_note = " a2a=on" if effective_a2a else ""
        self.reporter(
            f"Recovering removed instance '{spec.name}' from {spec.datadir} "
            f"(service={spec.service}, version {spec.version}, port {spec.port}, auth={spec.auth_mode}).{a2a_note}"
        )
        self.store.append_log(
            f"recreate removed instance name={spec.name} version={spec.version} fresh={fresh} timeout={timeout} a2a={effective_a2a}"
        )
        if spec.image_tag_override:
            prepared_image = spec.image_tag_override
            self.reporter(
                f"Reusing the saved runtime image {prepared_image} from instance metadata. "
                "Docker will pull it on container start if it is missing locally."
            )
        elif prepare_artifact:
            prepared_image = adapter.prepare_artifact(spec.version)
            if effective_a2a:
                prepared_image = self._bake_a2a_image(spec.service, spec.version, prepared_image)
        else:
            prepared_image = spec.image_tag_override or image_tag_for_service(spec.service, spec.version)
            if effective_a2a:
                prepared_image = a2a_image_tag(spec.service, spec.version, clawcu_version)
            self.reporter(
                f"Reusing the existing image tag {prepared_image} without re-running artifact preparation."
            )
        container_name = container_name_for_service(spec.service, spec.name)
        if timeout is not None:
            try:
                self.docker.stop_container(container_name, timeout=timeout)
            except Exception as exc:
                self.reporter(
                    f"Graceful stop failed during recreate (proceeding to force-remove): {exc}"
                )
        self.docker.remove_container(container_name, missing_ok=True)
        preview_record = build_instance_record(
            replace(spec, image_tag_override=prepared_image),
            status="removed",
            history=[],
        )
        if fresh:
            self._wipe_datadir(preview_record)
        history = [
            {
                "action": "recreate_requested",
                "timestamp": utc_now_iso(),
                "version": spec.version,
                "from_status": "removed",
                "clawcu_version": clawcu_version,
                "auth_mode": spec.auth_mode,
                "service": spec.service,
                "recovered_from": spec.datadir,
            }
        ]
        live_record = self._start_new_instance(
            replace(spec, image_tag_override=prepared_image),
            history=history,
            auto_port=True,
        )
        self.reporter(self._lifecycle_summary("recreated", live_record))
        return live_record

    def _wipe_datadir(self, record: InstanceRecord) -> None:
        """Remove all contents of ``record.datadir`` (used by recreate --fresh).

        The directory itself is preserved so the bind mount stays valid.
        Refuses to touch obviously unsafe paths (``/``, ``$HOME``, empty)
        as a defense-in-depth against a corrupted record — datadir
        normally lives under the managed root and is trusted, but the
        cost of an accidental HOME wipe is too high to skip the check.
        """
        datadir = Path(record.datadir).expanduser().resolve()
        unsafe = {Path("/").resolve(), Path.home().resolve()}
        if str(datadir) in {"", "."} or datadir in unsafe:
            raise ValueError(
                f"Refusing to wipe unsafe datadir for instance '{record.name}': {datadir}"
            )
        if not datadir.exists():
            return
        removed = 0
        for child in datadir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        self.reporter(f"Wiped datadir contents: {datadir} ({removed} entries removed)")

    def _service_manager(self, record: InstanceRecord):
        """Return the service-native manager (openclaw / hermes) for a record."""
        return self.openclaw if record.service == "openclaw" else self.hermes

    def upgrade_plan(
        self,
        name: str,
        *,
        version: str,
        image: str | None = None,
    ) -> dict:
        """Return an upgrade preview payload without touching Docker or disk.

        Used by `clawcu upgrade --dry-run` and by the confirmation prompt
        to show the user exactly what will change: current version →
        target, env path and whether it will be carried over, data
        directory, projected docker image tag, and where the safety
        snapshot will land.
        """
        record = self.store.load_record(name)
        adapter = self.adapter_for_record(record)
        target_version = normalize_service_version(record.service, version)
        projected_image = self._planned_runtime_image(
            record, version=target_version, image=image
        )
        if target_version == record.version and projected_image == record.image_tag:
            raise ValueError(
                f"Instance '{name}' is already on version {target_version}."
            )
        env_path = adapter.env_path(self, record)
        env_values = self._load_env_file(env_path) if env_path.exists() else {}
        snapshot_label = f"upgrade-to-{target_version}"
        snapshot_root = self.store.paths.snapshots_dir / record.name
        return {
            "instance": record.name,
            "service": record.service,
            "current_version": record.version,
            "target_version": target_version,
            "current_image": record.image_tag,
            "datadir": str(record.datadir),
            "env_path": str(env_path),
            "env_exists": env_path.exists(),
            "env_keys": sorted(env_values.keys()),
            "env_carryover": "preserved",
            "projected_image": projected_image,
            "snapshot_root": str(snapshot_root),
            "snapshot_label": snapshot_label,
        }

    def list_upgradable_versions(
        self, name: str, *, include_remote: bool = True
    ) -> dict:
        """Enumerate versions useful for `clawcu upgrade --list-versions`.

        Returns four buckets:
        - ``history``: every from/to version this instance has observed
          plus the current version (useful as "roll back candidates");
        - ``local_images``: tags of the configured image repo present on
          the local Docker daemon (these upgrades skip a pull);
        - ``remote_versions``: tag list returned by the configured
          image registry, filtered by the service's release-tag rule.
          ``None`` when ``include_remote=False`` or when the fetch
          failed (``remote_error`` carries the reason);
        - ``current_version`` for callers to annotate.

        ``include_remote=True`` (the default) means the remote registry
        is queried. The query is best-effort: network/auth failures are
        caught, surfaced via ``remote_error``, and do not fail the
        overall call. Pass ``include_remote=False`` for a strictly
        offline view (e.g. in CI or on airplanes).
        """
        record = self.store.load_record(name)
        manager = self._service_manager(record)
        repo = getattr(manager, "image_repo", "")
        history_set: set[str] = {record.version} if record.version else set()
        for entry in record.history or []:
            if isinstance(entry, dict):
                for key in ("from_version", "to_version", "version"):
                    value = entry.get(key)
                    if isinstance(value, str) and value:
                        history_set.add(value)
        history_set.discard("-")
        local_images = self.docker.list_local_images(repo) if repo else []
        # Sort by semver so the "Local images" section agrees with the
        # remote section. Non-semver tags (e.g. ``latest``) fall into the
        # low-sort bucket and stay at the top, labelled by the CLI.
        local_images = sorted(local_images, key=semver_sort_key)

        remote_versions: list[str] | None = None
        remote_error: str | None = None
        remote_registry: str | None = None
        if include_remote and repo and hasattr(manager, "list_remote_versions"):
            try:
                result = manager.list_remote_versions()
            except Exception as exc:  # defensive — manager is best-effort
                remote_error = f"unexpected error: {exc}"
            else:
                remote_registry = getattr(result, "registry", None) or None
                if result.ok:
                    remote_versions = result.tags or []
                else:
                    remote_error = result.error

        return {
            "instance": record.name,
            "service": record.service,
            "image_repo": repo,
            "current_version": record.version,
            "history": sorted(history_set, key=semver_sort_key),
            "local_images": local_images,
            "remote_versions": remote_versions,
            "remote_error": remote_error,
            "remote_registry": remote_registry,
            "remote_requested": include_remote,
        }

    _AVAILABLE_VERSIONS_CACHE_VERSION = 1

    def _available_versions_cache_path(self) -> Path:
        return self.store.paths.home / "cache" / "available_versions.json"

    def _load_available_versions_cache(self) -> dict:
        path = self._available_versions_cache_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(payload, dict):
            return {}
        if payload.get("version") != self._AVAILABLE_VERSIONS_CACHE_VERSION:
            return {}
        entries = payload.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _save_available_versions_cache(self, entries: dict) -> None:
        path = self._available_versions_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._AVAILABLE_VERSIONS_CACHE_VERSION,
            "entries": entries,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def list_service_available_versions(
        self, *, include_remote: bool = True
    ) -> dict:
        """Enumerate remote release tags per service, independent of any instance.

        Used by `clawcu list` to print a "what versions are out there"
        block alongside the instance table. Each entry carries the
        configured image repo, the filtered release-tag list (sorted
        oldest -> newest), and — on failure — an ``error`` string so
        the CLI can explain why a service's row is empty. The fetch is
        best-effort; network / auth failures are caught so a red badge
        day at the registry does not break the default `list` command.

        Successful fetches are cached on disk at
        ``<clawcu_home>/cache/available_versions.json`` and reused for
        the remainder of the local calendar day. A stale entry (either
        a previous day or an ``image_repo`` mismatch after ``setup``)
        triggers a fresh fetch. Failures are never cached so a transient
        registry outage does not linger. ``include_remote=False``
        bypasses both the fetch and the cache entirely — used by
        ``--no-remote`` for strictly offline rendering.
        """
        today = time.strftime("%Y-%m-%d")
        cache: dict = self._load_available_versions_cache() if include_remote else {}
        cache_changed = False
        out: dict[str, dict] = {}
        for name, manager in (
            ("openclaw", self.openclaw),
            ("hermes", self.hermes),
        ):
            repo = getattr(manager, "image_repo", "") or ""
            entry: dict = {
                "service": name,
                "image_repo": repo,
                "versions": None,
                "registry": None,
                "error": None,
                "local_versions": [],
                "remote_requested": include_remote,
            }
            if not (include_remote and repo and hasattr(manager, "list_remote_versions")):
                # Offline mode (`--no-remote` or no repo configured):
                # surface local docker images so the footer still has
                # something useful to say. Keep `remote_requested` so the
                # renderer can tell "user opted out" apart from "fetch failed".
                entry["local_versions"] = self._collect_local_versions(repo)
                out[name] = entry
                continue

            cached = cache.get(name)
            if (
                isinstance(cached, dict)
                and cached.get("fetched_date") == today
                and cached.get("image_repo") == repo
                and isinstance(cached.get("versions"), list)
            ):
                entry["versions"] = list(cached["versions"])
                entry["registry"] = cached.get("registry")
                out[name] = entry
                continue

            try:
                result = manager.list_remote_versions()
            except Exception as exc:  # defensive — best-effort only
                entry["error"] = f"unexpected error: {exc}"
            else:
                entry["registry"] = getattr(result, "registry", None) or None
                if result.ok:
                    # Drop prerelease tags (`-beta.1`, `-rc.2`, `-alpha`, …)
                    # from the "available versions" surface. Both services
                    # use `YYYY.M.P` for stable releases and append a
                    # hyphenated suffix for prereleases, so a simple
                    # "no hyphen" rule is exact enough. upgrade's own
                    # `--list-versions` still sees the full tag set via
                    # list_upgradable_versions for testers who want them.
                    entry["versions"] = [
                        tag for tag in (result.tags or []) if "-" not in tag
                    ]
                    cache[name] = {
                        "service": name,
                        "image_repo": repo,
                        "versions": entry["versions"],
                        "registry": entry["registry"],
                        "fetched_date": today,
                    }
                    cache_changed = True
                else:
                    entry["error"] = result.error
            if entry["versions"] is None:
                # Remote fetch failed — fall back to local docker images
                # so the user still sees something actionable instead of
                # just a red error line.
                entry["local_versions"] = self._collect_local_versions(repo)
            out[name] = entry
        if cache_changed:
            self._save_available_versions_cache(cache)
        return out

    def _collect_local_versions(self, repo: str) -> list[str]:
        if not repo:
            return []
        try:
            tags = self.docker.list_local_images(repo)
        except Exception:
            return []
        # Filter prereleases to match the remote-versions policy, sort by
        # semver so the "newest first" UI layer works uniformly.
        stable = [tag for tag in tags if "-" not in tag and tag != "latest"]
        return sorted(stable, key=semver_sort_key)

    def list_rollback_targets(self, name: str) -> dict:
        """Enumerate snapshot targets usable by `clawcu rollback --list`.

        Each reversible lifecycle transition (an ``upgrade`` or prior
        ``rollback`` event) in the instance history produced a safety
        snapshot of the data directory and env file. Restoring one of
        those snapshots restores the corresponding ``from_version``, so
        every entry surfaces both the action that produced the snapshot
        and the version it restores to.

        The returned list is ordered oldest -> newest so the UI can
        show the most recent restore point last (mirroring a shell
        history).
        """
        record = self.store.load_record(name)
        targets: list[dict] = []
        for index, event in enumerate(record.history or []):
            if not isinstance(event, dict):
                continue
            action = event.get("action")
            if action not in {"upgrade", "rollback"}:
                continue
            snapshot_dir = event.get("snapshot_dir")
            exists = bool(snapshot_dir) and Path(snapshot_dir).exists()
            targets.append(
                {
                    "index": index,
                    "action": action,
                    "timestamp": event.get("timestamp"),
                    "from_version": event.get("from_version"),
                    "to_version": event.get("to_version"),
                    "from_image": event.get("from_image"),
                    "to_image": event.get("to_image"),
                    "snapshot_dir": snapshot_dir,
                    "snapshot_exists": exists,
                    # Rolling back restores the state captured *before* the
                    # transition, which is recorded as ``from_version``.
                    "restores_to": event.get("from_version"),
                }
            )
        return {
            "instance": record.name,
            "service": record.service,
            "current_version": record.version,
            "targets": targets,
        }

    def rollback_plan(self, name: str, *, to_version: str | None = None) -> dict:
        """Return a rollback preview payload without touching Docker or disk.

        Mirrors ``upgrade_plan`` in shape so the CLI can render it with
        the same table. If ``to_version`` is omitted, the most recent
        reversible transition is selected (matching the default
        ``rollback_instance`` behavior).
        """
        record = self.store.load_record(name)
        adapter = self.adapter_for_record(record)
        transition = self._resolve_rollback_target(record, to_version=to_version)
        previous_version = normalize_service_version(
            record.service, transition["from_version"]
        )
        restore_from = transition.get("snapshot_dir")
        env_path = adapter.env_path(self, record)
        snapshot_label = f"rollback-from-{record.version}"
        snapshot_root = self.store.paths.snapshots_dir / record.name
        projected_image = (
            str(transition.get("from_image") or "").strip()
            or self._planned_runtime_image(record, version=previous_version)
        )
        return {
            "instance": record.name,
            "service": record.service,
            "current_version": record.version,
            "target_version": previous_version,
            "current_image": record.image_tag,
            "datadir": str(record.datadir),
            "env_path": str(env_path),
            "env_exists": env_path.exists(),
            "restore_snapshot": str(restore_from) if restore_from else None,
            "restore_snapshot_exists": bool(restore_from) and Path(restore_from).exists(),
            "selected_action": transition.get("action"),
            "selected_timestamp": transition.get("timestamp"),
            "projected_image": projected_image,
            "snapshot_root": str(snapshot_root),
            "snapshot_label": snapshot_label,
        }

    def _resolve_rollback_target(
        self, record: InstanceRecord, *, to_version: str | None
    ) -> dict:
        """Pick the history event that defines which snapshot we restore.

        Without ``to_version`` this behaves exactly like
        ``_latest_transition``. With ``to_version`` supplied, it scans
        history newest-first for the most recent event whose
        ``from_version`` (the pre-transition state) matches the
        requested version.
        """
        if to_version is None:
            return self._latest_transition(record)
        normalized = normalize_service_version(record.service, to_version)
        for event in reversed(record.history or []):
            if not isinstance(event, dict):
                continue
            if event.get("action") not in {"upgrade", "rollback"}:
                continue
            if event.get("from_version") == normalized:
                return event
        raise ValueError(
            f"Instance '{record.name}' has no rollback snapshot for version {normalized}. "
            "Run 'clawcu rollback <name> --list' to see available targets."
        )

    def upgrade_instance(
        self,
        name: str,
        *,
        version: str,
        image: str | None = None,
    ) -> InstanceRecord:
        record = self.store.load_record(name)
        adapter = self.adapter_for_record(record)
        target_version = normalize_service_version(record.service, version)
        explicit_image = self._normalize_requested_image(image)
        target_image = self._planned_runtime_image(
            record,
            version=target_version,
            image=explicit_image,
        )
        if target_version == record.version and target_image == record.image_tag:
            raise ValueError(f"Instance '{name}' is already on version {target_version}.")

        self.reporter(
            f"Step 1/4: Preparing an upgrade plan for '{record.name}'. "
            "This should take a second or two."
        )
        env_path = adapter.env_path(self, record)
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
            if explicit_image is not None:
                prepared_image = explicit_image
                self.reporter(
                    f"Step 3/4: Using explicit runtime image {prepared_image} for {adapter.display_name} {target_version}. "
                    "Docker will pull it on container start if it is missing locally."
                )
            else:
                self.reporter(
                    f"Step 3/4: Preparing {adapter.display_name} {target_version}. "
                    "This may take a while if the image or source artifact needs to be prepared."
                )
                prepared_image = adapter.prepare_artifact(target_version)
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
                f"Failed to prepare {adapter.display_name} {target_version}. Existing instance was left untouched."
            ) from exc

        previous = copy.deepcopy(record)
        upgraded = updated_record(
            record,
            version=target_version,
            upstream_ref=upstream_ref_for_service(record.service, target_version),
            image_tag=prepared_image,
            status="upgrading",
        )
        try:
            self.reporter(
                f"Step 4/4: Recreating the container on {adapter.display_name} {target_version} "
                "with the existing data directory."
            )
            self.docker.remove_container(previous.container_name, missing_ok=True)
            adapter.configure_before_run(self, upgraded)
            self._run_container(upgraded)
            upgraded = adapter.wait_for_readiness(self, self._persist_live_status(upgraded))
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
                adapter.configure_before_run(self, previous)
                self._run_container(previous)
                previous = adapter.wait_for_readiness(self, self._persist_live_status(previous))
            except Exception as nested_exc:
                rollback_error = nested_exc

            previous.history.append(
                {
                    "action": "upgrade_failed",
                    "timestamp": utc_now_iso(),
                    "from_version": previous.version,
                    "to_version": target_version,
                    "from_image": previous.image_tag,
                    "to_image": prepared_image,
                    "snapshot_dir": str(snapshot_dir),
                    "error": str(exc),
                    "rollback_error": str(rollback_error) if rollback_error else None,
                    "phase": "container_recreate",
                }
            )
            previous.status = self.docker.container_status(previous.container_name)
            previous.updated_at = utc_now_iso()
            self.store.save_record(previous)
            self._write_instance_metadata(previous)
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
                "from_image": previous.image_tag,
                "to_image": prepared_image,
                "snapshot_dir": str(snapshot_dir),
            }
        )
        self.store.save_record(upgraded)
        self._write_instance_metadata(upgraded)
        self.reporter(
            f"Upgrade snapshot retained at {snapshot_dir}. "
            f"Run 'clawcu rollback {upgraded.name}' if you want to restore {previous.version}."
        )
        self.reporter(self._lifecycle_summary("upgraded", upgraded))
        return upgraded

    def rollback_instance(
        self, name: str, *, to_version: str | None = None
    ) -> InstanceRecord:
        record = self.store.load_record(name)
        adapter = self.adapter_for_record(record)
        transition = self._resolve_rollback_target(record, to_version=to_version)
        previous_version = normalize_service_version(record.service, transition["from_version"])
        restore_from = transition.get("snapshot_dir")

        self.reporter(
            f"Step 1/4: Preparing to roll back '{record.name}' from {record.version} to {previous_version}. "
            "This should take a second or two."
        )
        self.store.append_log(
            f"rollback instance name={record.name} from={record.version} to={previous_version}"
        )
        historical_image = str(transition.get("from_image") or "").strip()
        if historical_image:
            prepared_image = historical_image
            self.reporter(
                f"Step 2/4: Reusing recorded runtime image {prepared_image} for {adapter.display_name} {previous_version}. "
                "Docker will pull it on container start if it is missing locally."
            )
        else:
            self.reporter(
                f"Step 2/4: Preparing {adapter.display_name} {previous_version}. "
                "This may take a while if the image or source artifact is not available locally."
            )
            prepared_image = adapter.prepare_artifact(previous_version)
        env_path = adapter.env_path(self, record)
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
            upstream_ref=upstream_ref_for_service(record.service, previous_version),
            image_tag=prepared_image,
            status="rolling-back",
        )
        rolled.history.append(
            {
                "action": "rollback",
                "timestamp": utc_now_iso(),
                "from_version": record.version,
                "to_version": previous_version,
                "from_image": record.image_tag,
                "to_image": prepared_image,
                "snapshot_dir": str(current_snapshot),
                "restored_snapshot": restore_from,
            }
        )
        self.reporter(
            f"Step 4/4: Starting {adapter.display_name} {previous_version} "
            "with the restored data directory and env file."
        )
        adapter.configure_before_run(self, rolled)
        self._run_container(rolled)
        rolled = adapter.wait_for_readiness(self, self._persist_live_status(rolled))
        self.store.save_record(rolled)
        self._write_instance_metadata(rolled)
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
        version: str | None = None,
        include_secrets: bool = True,
    ) -> InstanceRecord:
        """Clone an existing instance into a fresh one.

        By default the clone mirrors the source exactly — same version,
        and the source's env file (which typically holds API keys /
        tokens) is copied into the clone. Two knobs adjust that:

        - ``version``: switch the clone to a different service version
          at copy time (handy for "clone, then upgrade" experiments).
          The clone's history records both the source name and, when
          the version differs from the source, the source version for
          provenance.
        - ``include_secrets`` (default ``True``): when ``False``, the
          source env file is NOT propagated. The clone boots with an
          empty env and the user re-authenticates / re-configures it
          explicitly — safer when the clone is meant for sharing or
          for a different user / key scope.
        """
        self.reporter("Step 1/5: Validating the source instance and resolving clone defaults. This should take a second or two.")
        source = self.store.load_record(source_name)
        adapter = self.adapter_for_record(source)
        target_version = version or source.version
        clone_spec = adapter.build_spec(
            self,
            name=name,
            version=target_version,
            datadir=datadir,
            port=port,
            cpu=source.cpu,
            memory=source.memory,
        )
        if self.store.instance_path(clone_spec.name).exists():
            raise ValueError(f"Instance '{clone_spec.name}' already exists.")
        container_name = container_name_for_service(clone_spec.service, clone_spec.name)
        if self.docker.container_status(container_name) != "missing":
            raise ValueError(
                f"Instance '{clone_spec.name}' already exists. Docker container '{container_name}' is already present."
            )

        target_dir = Path(clone_spec.datadir)
        if target_dir.exists():
            raise ValueError(f"Target datadir '{target_dir}' already exists.")
        preview_record = build_instance_record(clone_spec, status="creating", history=[])
        source_env_path = adapter.env_path(self, source)
        target_env_path = adapter.env_path(self, preview_record)
        try:
            self.reporter(
                f"Resolved clone settings: datadir={clone_spec.datadir}, port={clone_spec.port}, cpu={clone_spec.cpu}, memory={clone_spec.memory}."
            )
            self.reporter("Step 2/5: Copying the source data directory into a new experiment directory. This can take a while for larger instances.")
            shutil.copytree(source.datadir, target_dir)
            env_external = (
                source_env_path.exists()
                and not self._env_path_within_datadir(source_env_path, Path(source.datadir))
            )
            if not include_secrets:
                # Explicit opt-out: never propagate the source env
                # (API keys / tokens). The clone still boots; the user
                # re-authenticates with `setenv` or the service's
                # native config flow.
                self.reporter(
                    "Step 3/5: --exclude-secrets specified. Skipping env copy — the "
                    "clone will start without the source's API keys / tokens. Use "
                    "`clawcu setenv` or the service's native config flow to seed new "
                    "credentials."
                )
            elif env_external:
                self.reporter("Step 3/5: Copying the instance environment variables. This usually takes a second or two.")
                target_env_path.parent.mkdir(parents=True, exist_ok=True)
                target_env_path.write_text(source_env_path.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.reporter("Step 3/5: No instance environment file was found on the source instance. Skipping env copy.")
            self.reporter(f"Step 4/5: Making sure the requested {adapter.display_name} artifact is available.")
            prepared_image = adapter.prepare_artifact(clone_spec.version)
            # Re-resolve image_tag from current image_repo config rather than
            # inheriting the source record's tag, which may point at a repo
            # the user has since migrated away from.
            clone_spec = replace(clone_spec, image_tag_override=prepared_image)
            self.reporter("Step 5/5: Starting the cloned Docker container and checking health. This usually takes a few seconds.")
            history_event: dict = {
                "action": "cloned",
                "timestamp": utc_now_iso(),
                "from_instance": source.name,
                "to_version": target_version,
                "secrets_included": bool(include_secrets),
            }
            if target_version != source.version:
                history_event["from_source_version"] = source.version
            record = self._start_new_instance(
                clone_spec,
                history=[history_event],
                auto_port=port is None,
            )
        except Exception:
            self.docker.remove_container(container_name, missing_ok=True)
            self.store.delete_record(clone_spec.name)
            if target_env_path.exists() and not self._env_path_within_datadir(target_env_path, target_dir):
                target_env_path.unlink()
            if target_dir.exists():
                shutil.rmtree(target_dir)
            raise
        self.store.append_log(
            "clone instance "
            f"source={source.name} target={record.name} datadir={record.datadir} "
            f"version={record.version} secrets={'yes' if include_secrets else 'no'}"
        )
        return record

    def stream_logs(
        self,
        name: str,
        *,
        follow: bool = False,
        tail: int | None = None,
        since: str | None = None,
    ) -> None:
        record = self.store.load_record(name)
        self.docker.stream_logs(
            record.container_name,
            follow=follow,
            tail=tail,
            since=since,
        )

    def remove_instance(self, name: str, *, delete_data: bool = False) -> None:
        record = self.store.load_record(name)
        adapter = self.adapter_for_record(record)
        try:
            self.docker.remove_container(record.container_name, missing_ok=True)
        except Exception as exc:
            message = (
                f"Failed to remove Docker container '{record.container_name}' for instance "
                f"'{record.name}': {exc}"
            )
            failed = updated_record(record, last_error=message)
            failed.history.append(
                {
                    "action": "remove_failed",
                    "timestamp": utc_now_iso(),
                    "version": record.version,
                    "error": str(exc),
                }
            )
            self.store.save_record(failed)
            raise RuntimeError(message) from exc
        if delete_data and Path(record.datadir).exists():
            shutil.rmtree(record.datadir)
        if delete_data:
            env_path = adapter.env_path(self, record)
            if env_path.exists() and not self._env_path_within_datadir(env_path, Path(record.datadir)):
                env_path.unlink()
        self.store.delete_record(record.name)
        self.store.append_log(
            f"remove instance name={record.name} delete_data={'yes' if delete_data else 'no'}"
        )

    def remove_removed_instance(self, name: str) -> None:
        root = self._find_removed_instance_root(name)
        if root is None:
            raise FileNotFoundError(
                f"Removed instance '{name}' was not found. Run `clawcu list --removed` to see recoverable leftovers."
            )
        service_name: str | None = None
        metadata = self._load_instance_metadata(root)
        hinted_service = metadata.get("service")
        if isinstance(hinted_service, str) and hinted_service in self.adapters:
            service_name = hinted_service
        if service_name is None:
            for adapter in self.adapters.values():
                if adapter.removed_instance_summary(self, root) is not None:
                    service_name = adapter.service_name
                    break
        try:
            if service_name is not None:
                container_name = container_name_for_service(service_name, name)
                self.docker.remove_container(container_name, missing_ok=True)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to remove Docker container for removed instance '{name}': {exc}"
            ) from exc
        shutil.rmtree(root)
        self.store.append_log(f"remove removed-instance name={name} datadir={root}")

    def _next_available_port(self, start_port: int | None = None) -> int:
        port = start_port if start_port is not None else self.DEFAULT_OPENCLAW_PORT
        for _ in range(self.PORT_SEARCH_LIMIT):
            if self._is_port_available(port):
                return port
            port += self.PORT_SEARCH_STEP
        raise RuntimeError("Could not find a free port in the configured search range.")

    def _is_port_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                return False
        return True

    def _start_new_instance(
        self,
        spec: InstanceSpec,
        *,
        history: list[dict],
        auto_port: bool,
        env_overrides: dict[str, str] | None = None,
    ) -> InstanceRecord:
        current_spec = spec
        current_history = copy.deepcopy(history)
        while True:
            adapter = self.adapter_for_service(current_spec.service)
            record = build_instance_record(
                current_spec,
                status="creating",
                history=copy.deepcopy(current_history),
            )
            self.store.save_record(record)
            self._write_instance_metadata(record)
            try:
                adapter.configure_before_run(self, record)
                if env_overrides:
                    self._apply_env_overrides(adapter, record, env_overrides)
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
                next_port = self._next_available_port(current_spec.port + self.PORT_SEARCH_STEP)
                next_dashboard_port = current_spec.dashboard_port
                if current_spec.dashboard_port is not None:
                    next_dashboard_port = self._next_available_port(
                        current_spec.dashboard_port + self.PORT_SEARCH_STEP
                    )
                    while next_dashboard_port == next_port:
                        next_dashboard_port = self._next_available_port(
                            next_dashboard_port + self.PORT_SEARCH_STEP
                        )
                    self.reporter(
                        "Port conflict detected before Docker could bind the instance. "
                        f"Retrying with API port {next_port} and dashboard port {next_dashboard_port}."
                    )
                else:
                    self.reporter(
                        f"Port {current_spec.port} was claimed before Docker could bind it. Retrying with port {next_port}."
                    )
                current_history = copy.deepcopy(failure.history)
                current_spec = replace(
                    current_spec,
                    port=next_port,
                    dashboard_port=next_dashboard_port,
                )
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
                return adapter.wait_for_readiness(self, live_record)
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
        record = self.store.load_record(instance_name)
        env_path = self.adapter_for_record(record).env_path(self, record)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        api_key = self._provider_bundle_api_key(bundle)
        if not isinstance(api_key, str) or not api_key.strip():
            return None

        env_key = self._provider_env_key(provider_name)
        env_values = self._load_env_file(env_path)
        env_values[env_key] = api_key.strip()
        env_path.write_text(self._dump_env_file(env_values), encoding="utf-8")
        return env_key

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
        self, bundle: dict[str, object], *, overwrite: bool = False
    ) -> tuple[str, str]:
        service_name = str(bundle["service"])
        base_name = str(bundle["name"])
        if not self.store.provider_exists(service_name, base_name):
            self.store.save_provider_bundle(service_name, base_name, bundle)
            return base_name, "saved"

        candidate_names = [
            name
            for candidate_service, name in self.store.list_provider_refs()
            if candidate_service == service_name and (name == base_name or name.startswith(f"{base_name}-"))
        ]
        for candidate in candidate_names:
            existing = self.store.load_provider_bundle(service_name, candidate)
            if self._provider_bundle_equals(existing, bundle):
                if overwrite:
                    self.store.save_provider_bundle(service_name, candidate, bundle)
                    return candidate, "overwritten"
                return candidate, "skipped"

            if self._provider_signature(existing) == self._provider_signature(bundle):
                if overwrite:
                    self.store.save_provider_bundle(service_name, candidate, bundle)
                    return candidate, "overwritten"
                if service_name == "openclaw":
                    merged = self._merge_service_provider_bundles(existing, bundle)
                    self.store.save_provider_bundle(service_name, candidate, merged)
                    return candidate, "merged"

        suffix = 2
        while True:
            candidate = f"{base_name}-{suffix}"
            if not self.store.provider_exists(service_name, candidate):
                self.store.save_provider_bundle(service_name, candidate, bundle)
                return candidate, "saved"
            suffix += 1

    def _provider_bundle_equals(self, existing: dict[str, object], incoming: dict[str, object]) -> bool:
        keys = ("metadata", "auth_profiles", "models", "config_yaml", "env", "auth_json")
        return {key: existing.get(key) for key in keys} == {key: incoming.get(key) for key in keys}

    def _provider_signature(self, bundle: dict[str, object]) -> tuple[str, str, str | None, str | None]:
        metadata = bundle.get("metadata", {})
        provider_name = str(bundle.get("name") or "")
        endpoint = None
        api_style = "openai"
        if isinstance(metadata, dict):
            provider_name = str(metadata.get("provider") or provider_name)
            api_style = str(metadata.get("api_style") or api_style)
            raw_endpoint = metadata.get("endpoint")
            if isinstance(raw_endpoint, str) and raw_endpoint.strip():
                endpoint = raw_endpoint.strip()
        return provider_name, api_style, endpoint, self._provider_bundle_api_key(bundle)

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

    def _provider_bundle_api_key(self, bundle: dict[str, object]) -> str | None:
        service_name = str(bundle.get("service") or "")
        if service_name == "openclaw":
            auth_payload = bundle.get("auth_profiles", {})
            models_payload = bundle.get("models", {})
            if isinstance(auth_payload, dict) and isinstance(models_payload, dict):
                return self._bundle_api_key(auth_payload, models_payload)
            return None
        env_payload = str(bundle.get("env") or "")
        env_values = self._load_env_text(env_payload)
        preferred = [key for key in sorted(env_values) if key.endswith("_API_KEY") or key.endswith("_TOKEN")]
        if preferred:
            return env_values[preferred[0]]
        for value in env_values.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _provider_bundle_api_key_state(
        self, bundle: dict[str, object]
    ) -> str:
        """Classify the api_key source so the list view can distinguish
        ``set`` (literal key present), ``env-ref`` (placeholder like
        ``${OPENAI_API_KEY}``), ``empty`` (field present but blank — a
        captured template), and ``missing`` (no source at all).
        """
        key = self._provider_bundle_api_key(bundle)
        if isinstance(key, str) and key.strip():
            stripped = key.strip()
            if stripped.startswith("${") and stripped.endswith("}"):
                return "env-ref"
            if stripped.startswith("$") and not stripped.startswith("$$"):
                return "env-ref"
            return "set"
        # No usable key value came back. Tell apart "the source had the
        # field but it was blank" (captured empty) from "no apiKey field
        # anywhere in the source" (missing).
        service_name = str(bundle.get("service") or "")
        if service_name == "openclaw":
            models_payload = bundle.get("models", {})
            if isinstance(models_payload, dict):
                _, provider_payload = self._single_provider_entry(models_payload)
                if isinstance(provider_payload, dict) and "apiKey" in provider_payload:
                    return "empty"
            auth_payload = bundle.get("auth_profiles", {})
            if isinstance(auth_payload, dict):
                profiles = auth_payload.get("profiles") or {}
                if isinstance(profiles, dict):
                    for profile in profiles.values():
                        if not isinstance(profile, dict):
                            continue
                        if "key" in profile or "apiKey" in profile:
                            return "empty"
            return "missing"
        env_values = self._load_env_text(str(bundle.get("env") or ""))
        if env_values:
            # env file had entries but none yielded a usable key value
            return "empty"
        return "missing"

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

    def _merge_service_provider_bundles(
        self,
        existing: dict[str, object],
        incoming: dict[str, object],
    ) -> dict[str, object]:
        service_name = str(existing.get("service") or incoming.get("service") or "")
        if service_name != "openclaw":
            return incoming
        merged_auth, merged_models = self._merge_provider_bundles(
            {
                "auth_profiles": dict(existing.get("auth_profiles", {})),
                "models": dict(existing.get("models", {})),
            },
            {
                "auth_profiles": dict(incoming.get("auth_profiles", {})),
                "models": dict(incoming.get("models", {})),
            },
        )
        merged = copy.deepcopy(existing)
        merged["metadata"] = copy.deepcopy(incoming.get("metadata", existing.get("metadata", {})))
        merged["auth_profiles"] = merged_auth
        merged["models"] = merged_models
        return merged

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

    def active_provider_for_instance(self, name: str) -> str | None:
        """Return the provider name that is currently the default on an
        instance (parsed from ``agents/<agent>/agent/models.json``), or
        ``None`` when no signal is available.

        Resolution order, best → least specific:
        1. ``agents.defaults.model.primary`` — the ``provider/model`` pair
           the UI hands to new sessions.
        2. First provider key in the ``models.providers`` dict.

        Returns ``None`` for non-openclaw services or when models.json is
        missing / malformed, so callers treat it as a hint, not a
        guarantee.
        """
        try:
            record = self.store.load_record(name)
        except Exception:
            return None
        if record.service != "openclaw":
            return None
        datadir = Path(record.datadir)
        if not datadir.exists():
            return None
        for agent_name in self._managed_agent_names(datadir):
            runtime_dir = datadir / "agents" / agent_name / "agent"
            models_path = runtime_dir / "models.json"
            if not models_path.exists():
                continue
            try:
                models_payload = self._load_json_file(models_path)
            except Exception:
                continue
            config = {"models": models_payload, "agents": models_payload.get("agents", {})}
            primary, _ = self._configured_default_agent_model(config)
            if primary and primary != "-" and "/" in primary:
                candidate = primary.split("/", 1)[0].strip()
                if candidate:
                    return candidate
            # Fall back to the first provider in insertion order — that is
            # typically the one the user registered first / is using, which
            # is more useful than an alphabetical pick.
            providers = models_payload.get("providers")
            if isinstance(providers, dict):
                for provider_name, payload in providers.items():
                    if isinstance(provider_name, str) and provider_name.strip() and isinstance(payload, dict):
                        return provider_name.strip()
        return None

    def _host_healthcheck_ready(self, record: InstanceRecord) -> bool:
        adapter = self.adapter_for_record(record)
        check = getattr(adapter, "_host_healthcheck_ready", None)
        if callable(check):
            return bool(check(record))
        access = adapter.access_info(self, record)
        return bool(access.base_url)

    def _run_container(self, record: InstanceRecord) -> None:
        adapter = self.adapter_for_record(record)
        self.docker.run_container(record, adapter.run_spec(self, record))

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

    def _env_path_within_datadir(self, env_path: Path, datadir: Path) -> bool:
        try:
            env_path.resolve().relative_to(datadir.resolve())
            return True
        except ValueError:
            return False

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
                source = event.get("from_version") or "-"
                target = event.get("to_version") or "-"
                return f"rollback {source} -> {target}"
            if action == "upgrade":
                source = event.get("from_version") or "-"
                target = event.get("to_version") or "-"
                return f"upgrade {source} -> {target}"
        return "-"

    def _lifecycle_summary(self, action: str, record: InstanceRecord) -> str:
        return self.adapter_for_record(record).lifecycle_summary(self, action, record)
