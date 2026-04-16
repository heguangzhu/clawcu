from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from clawcu.core.models import InstanceRecord
from clawcu.core.paths import ClawCUPaths, bootstrap_config_path, build_paths, get_paths


class StateStore:
    def __init__(self, paths: ClawCUPaths | None = None):
        self.paths = paths or get_paths()

    def load_bootstrap_config(self) -> dict:
        path = bootstrap_config_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def save_bootstrap_config(self, payload: dict) -> None:
        path = bootstrap_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def get_bootstrap_home(self) -> str | None:
        value = self.load_bootstrap_config().get("clawcu_home")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def set_bootstrap_home(self, home: str) -> None:
        payload = self.load_bootstrap_config()
        payload["clawcu_home"] = home
        self.save_bootstrap_config(payload)

    def switch_home(self, home: str) -> None:
        self.paths = build_paths(Path(home))

    def load_config(self) -> dict:
        path = self.paths.config_path
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def save_config(self, payload: dict) -> None:
        self.paths.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _get_string_config(self, key: str) -> str | None:
        value = self.load_config().get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _set_string_config(self, key: str, value: str) -> None:
        payload = self.load_config()
        payload[key] = value
        self.save_config(payload)

    def get_openclaw_image_repo(self) -> str | None:
        return self._get_string_config("openclaw_image_repo")

    def set_openclaw_image_repo(self, image_repo: str) -> None:
        self._set_string_config("openclaw_image_repo", image_repo)

    def get_hermes_image_repo(self) -> str | None:
        return self._get_string_config("hermes_image_repo")

    def set_hermes_image_repo(self, image_repo: str) -> None:
        self._set_string_config("hermes_image_repo", image_repo)

    def instance_path(self, name: str) -> Path:
        return self.paths.instances_dir / f"{name}.json"

    def save_record(self, record: InstanceRecord) -> None:
        payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        self.instance_path(record.name).write_text(payload + "\n", encoding="utf-8")

    def load_record(self, name: str) -> InstanceRecord:
        path = self.instance_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Instance '{name}' was not found.")
        data = json.loads(path.read_text(encoding="utf-8"))
        return InstanceRecord.from_dict(data)

    def list_records(self) -> list[InstanceRecord]:
        records: list[InstanceRecord] = []
        for path in sorted(self.paths.instances_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(InstanceRecord.from_dict(data))
        return records

    def delete_record(self, name: str) -> None:
        path = self.instance_path(name)
        if path.exists():
            path.unlink()

    def source_dir(self, service: str, version: str) -> Path:
        path = self.paths.sources_dir / service / version
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def instance_env_path(self, name: str) -> Path:
        return self.paths.instances_dir / f"{name}.env"

    def provider_root_dir(self, service: str) -> Path:
        root = self.paths.providers_dir / service
        root.mkdir(parents=True, exist_ok=True)
        return root

    def provider_dir(self, service: str, name: str) -> Path:
        return self.provider_root_dir(service) / name

    def provider_metadata_path(self, service: str, name: str) -> Path:
        return self.provider_dir(service, name) / "metadata.json"

    def provider_auth_profiles_path(self, service: str, name: str) -> Path:
        return self.provider_dir(service, name) / "auth-profiles.json"

    def provider_models_path(self, service: str, name: str) -> Path:
        return self.provider_dir(service, name) / "models.json"

    def provider_config_path(self, service: str, name: str) -> Path:
        return self.provider_dir(service, name) / "config.yaml"

    def provider_env_path(self, service: str, name: str) -> Path:
        return self.provider_dir(service, name) / ".env"

    def _provider_ref_args(self, service_or_name: str, name: str | None = None) -> tuple[str, str]:
        if name is None:
            return "openclaw", service_or_name
        return service_or_name, name

    def provider_exists(self, service: str, name: str | None = None) -> bool:
        service_name, provider_name = self._provider_ref_args(service, name)
        return self.provider_dir(service_name, provider_name).is_dir()

    def list_provider_refs(self) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        if not self.paths.providers_dir.exists():
            return refs
        for service_dir in sorted(path for path in self.paths.providers_dir.iterdir() if path.is_dir()):
            for provider_dir in sorted(path for path in service_dir.iterdir() if path.is_dir()):
                refs.append((service_dir.name, provider_dir.name))
        return refs

    def list_provider_names(self) -> list[str]:
        return [name for service, name in self.list_provider_refs() if service == "openclaw"]

    def load_provider_bundle(self, service: str, name: str | None = None) -> dict[str, object]:
        service_name, provider_name = self._provider_ref_args(service, name)
        provider_dir = self.provider_dir(service_name, provider_name)
        metadata_path = self.provider_metadata_path(service_name, provider_name)
        if not provider_dir.exists() or not metadata_path.exists():
            raise FileNotFoundError(f"Provider '{service_name}:{provider_name}' was not found.")
        bundle: dict[str, object] = {
            "service": service_name,
            "name": provider_name,
            "metadata": json.loads(metadata_path.read_text(encoding="utf-8")),
        }
        auth_path = self.provider_auth_profiles_path(service_name, provider_name)
        models_path = self.provider_models_path(service_name, provider_name)
        config_path = self.provider_config_path(service_name, provider_name)
        env_path = self.provider_env_path(service_name, provider_name)
        if auth_path.exists():
            bundle["auth_profiles"] = json.loads(auth_path.read_text(encoding="utf-8"))
        if models_path.exists():
            bundle["models"] = json.loads(models_path.read_text(encoding="utf-8"))
        if config_path.exists():
            bundle["config_yaml"] = config_path.read_text(encoding="utf-8")
        if env_path.exists():
            bundle["env"] = env_path.read_text(encoding="utf-8")
        return bundle

    def save_provider_bundle(self, service: str, name: str | dict[str, object], payload: dict[str, object] | None = None) -> None:
        if payload is None:
            raise TypeError("save_provider_bundle() requires an explicit payload.")
        service_name, provider_name = self._provider_ref_args(service, str(name))
        provider_dir = self.provider_dir(service_name, provider_name)
        provider_dir.mkdir(parents=True, exist_ok=True)
        metadata = dict(payload.get("metadata", {}))
        metadata.setdefault("service", service_name)
        metadata.setdefault("name", provider_name)
        self.provider_metadata_path(service_name, provider_name).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if "auth_profiles" in payload:
            self.provider_auth_profiles_path(service_name, provider_name).write_text(
                json.dumps(payload["auth_profiles"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if "models" in payload:
            self.provider_models_path(service_name, provider_name).write_text(
                json.dumps(payload["models"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if "config_yaml" in payload:
            self.provider_config_path(service_name, provider_name).write_text(
                str(payload["config_yaml"]),
                encoding="utf-8",
            )
        if "env" in payload:
            self.provider_env_path(service_name, provider_name).write_text(
                str(payload["env"]),
                encoding="utf-8",
            )

    def delete_provider(self, service: str, name: str | None = None) -> None:
        service_name, provider_name = self._provider_ref_args(service, name)
        provider_dir = self.provider_dir(service_name, provider_name)
        if provider_dir.exists():
            shutil.rmtree(provider_dir)

    def create_snapshot(
        self,
        name: str,
        datadir: Path,
        label: str,
        *,
        env_path: Path | None = None,
    ) -> Path:
        return self.create_snapshot_bundle(name, datadir, label, env_path=env_path)

    def create_snapshot_bundle(
        self,
        name: str,
        datadir: Path,
        label: str,
        *,
        env_path: Path | None = None,
    ) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_label = label.replace("/", "-").replace(" ", "-")
        snapshot_dir = self.paths.snapshots_dir / name / f"{timestamp}-{safe_label}"
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        if datadir.exists():
            shutil.copytree(datadir, snapshot_dir)
        else:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
        if env_path is not None and env_path.exists():
            self.snapshot_env_path(snapshot_dir).write_text(
                env_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        return snapshot_dir

    def restore_snapshot(
        self,
        snapshot_dir: Path,
        datadir: Path,
        *,
        env_path: Path | None = None,
    ) -> None:
        self.restore_snapshot_bundle(snapshot_dir, datadir, env_path=env_path)

    def restore_snapshot_bundle(
        self,
        snapshot_dir: Path,
        datadir: Path,
        *,
        env_path: Path | None = None,
    ) -> None:
        if datadir.exists():
            shutil.rmtree(datadir)
        shutil.copytree(snapshot_dir, datadir)
        if env_path is not None:
            snapshot_env = self.snapshot_env_path(snapshot_dir)
            if snapshot_env.exists():
                env_path.parent.mkdir(parents=True, exist_ok=True)
                env_path.write_text(snapshot_env.read_text(encoding="utf-8"), encoding="utf-8")
            elif env_path.exists():
                env_path.unlink()

    def snapshot_env_path(self, snapshot_dir: Path) -> Path:
        return snapshot_dir.with_name(f"{snapshot_dir.name}.env")

    def append_log(self, message: str) -> None:
        log_file = self.paths.logs_dir / "clawcu.log"
        timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
