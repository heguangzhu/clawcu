from __future__ import annotations

import copy
import socket
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Callable

from clawcu.docker import DockerManager
from clawcu.models import InstanceRecord, InstanceSpec
from clawcu.openclaw import OpenClawManager
from clawcu.storage import StateStore
from clawcu.subprocess_utils import CommandError
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
    Reporter = Callable[[str], None]

    def __init__(
        self,
        store: StateStore | None = None,
        docker: DockerManager | None = None,
        openclaw: OpenClawManager | None = None,
        reporter: Reporter | None = None,
    ):
        self.store = store or StateStore()
        self.docker = docker or DockerManager()
        self.reporter = reporter or (lambda _message: None)
        self.openclaw = openclaw or OpenClawManager(self.store, self.docker, reporter=self.reporter)
        self.set_reporter(self.reporter)

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)
        if hasattr(self.openclaw, "set_reporter"):
            self.openclaw.set_reporter(self.reporter)

    def pull_openclaw(self, version: str) -> str:
        normalized = normalize_version(version)
        self.reporter(
            f"Starting OpenClaw image preparation for version {normalized}. ClawCU will try the official image first, then fall back to a local build if needed."
        )
        self.store.append_log(f"pull openclaw version={normalized}")
        image_tag = self.openclaw.ensure_image(normalized)
        self.store.append_log(f"built image {image_tag}")
        self.reporter(f"Finished building Docker image {image_tag}.")
        return image_tag

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
            f"Resolved instance settings: datadir={spec.datadir}, port={spec.port}, cpu={spec.cpu}, memory={spec.memory}."
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
            }
        ]
        self.reporter("Step 5/5: Starting the Docker container and checking health. This usually takes a few seconds.")
        live_record = self._start_new_instance(spec, history=history, auto_port=auto_port)
        self.reporter(
            f"Instance '{live_record.name}' is ready. OpenClaw {live_record.version} is running on port {live_record.port}."
        )
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

    def inspect_instance(self, name: str) -> dict:
        record = self._persist_live_status(self.store.load_record(name))
        inspection = self.docker.inspect_container(record.container_name)
        return {"instance": record.to_dict(), "container": inspection}

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
        self.reporter(
            f"Instance '{live_record.name}' is ready again. OpenClaw {live_record.version} is running on port {live_record.port}."
        )
        return live_record

    def upgrade_instance(self, name: str, *, version: str) -> InstanceRecord:
        record = self.store.load_record(name)
        target_version = normalize_version(version)
        if target_version == record.version:
            raise ValueError(f"Instance '{name}' is already on version {target_version}.")

        snapshot_dir = self.store.create_snapshot(
            record.name,
            Path(record.datadir),
            f"upgrade-to-{target_version}",
        )
        self.store.append_log(
            f"upgrade instance name={record.name} from={record.version} to={target_version} snapshot={snapshot_dir}"
        )

        try:
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
            self.docker.remove_container(previous.container_name, missing_ok=True)
            self.docker.run_container(upgraded)
        except Exception as exc:
            rollback_error = None
            try:
                self.docker.remove_container(previous.container_name, missing_ok=True)
                if snapshot_dir.exists():
                    self.store.restore_snapshot(snapshot_dir, Path(previous.datadir))
                self.docker.run_container(previous)
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
        return self._persist_live_status(upgraded)

    def rollback_instance(self, name: str) -> InstanceRecord:
        record = self.store.load_record(name)
        transition = self._latest_transition(record)
        previous_version = normalize_version(transition["from_version"])
        restore_from = transition.get("snapshot_dir")

        self.store.append_log(
            f"rollback instance name={record.name} from={record.version} to={previous_version}"
        )
        self.openclaw.ensure_image(previous_version)
        current_snapshot = self.store.create_snapshot(
            record.name,
            Path(record.datadir),
            f"rollback-from-{record.version}",
        )

        self.docker.remove_container(record.container_name, missing_ok=True)
        if restore_from and Path(restore_from).exists():
            self.store.restore_snapshot(Path(restore_from), Path(record.datadir))

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
        self.docker.run_container(rolled)
        self.store.save_record(rolled)
        return self._persist_live_status(rolled)

    def clone_instance(
        self,
        source_name: str,
        *,
        name: str,
        datadir: str,
        port: int,
    ) -> InstanceRecord:
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

        target_dir = Path(clone_spec.datadir)
        if target_dir.exists():
            raise ValueError(f"Target datadir '{target_dir}' already exists.")
        shutil.copytree(source.datadir, target_dir)
        self.openclaw.ensure_image(clone_spec.version)

        record = build_instance_record(
            clone_spec,
            status="creating",
            history=[
                {
                    "action": "cloned",
                    "timestamp": utc_now_iso(),
                    "from_instance": source.name,
                    "to_version": source.version,
                }
            ],
        )
        self.docker.run_container(record)
        self.store.append_log(
            f"clone instance source={source.name} target={record.name} datadir={record.datadir}"
        )
        return self._persist_live_status(record)

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
        )

    def _default_datadir(self, name: str) -> str:
        return str((self.store.paths.home / name).resolve())

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
                self.docker.run_container(record)
                record.history.append(
                    {
                        "action": "created",
                        "timestamp": utc_now_iso(),
                        "version": record.version,
                        "port": record.port,
                    }
                )
                return self._persist_live_status(record)
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
