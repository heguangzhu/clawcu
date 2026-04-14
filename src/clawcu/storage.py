from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from clawcu.models import InstanceRecord
from clawcu.paths import ClawCUPaths, get_paths


class StateStore:
    def __init__(self, paths: ClawCUPaths | None = None):
        self.paths = paths or get_paths()

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

    def provider_dir(self, name: str) -> Path:
        return self.paths.providers_dir / name

    def provider_auth_profiles_path(self, name: str) -> Path:
        return self.provider_dir(name) / "auth-profiles.json"

    def provider_models_path(self, name: str) -> Path:
        return self.provider_dir(name) / "models.json"

    def provider_exists(self, name: str) -> bool:
        return self.provider_dir(name).is_dir()

    def list_provider_names(self) -> list[str]:
        return sorted(path.name for path in self.paths.providers_dir.iterdir() if path.is_dir())

    def load_provider_bundle(self, name: str) -> dict[str, dict]:
        auth_path = self.provider_auth_profiles_path(name)
        models_path = self.provider_models_path(name)
        if not auth_path.exists() or not models_path.exists():
            raise FileNotFoundError(f"Provider '{name}' was not found.")
        return {
            "name": name,
            "auth_profiles": json.loads(auth_path.read_text(encoding="utf-8")),
            "models": json.loads(models_path.read_text(encoding="utf-8")),
        }

    def save_provider_bundle(self, name: str, auth_payload: dict, models_payload: dict) -> None:
        provider_dir = self.provider_dir(name)
        provider_dir.mkdir(parents=True, exist_ok=True)
        self.provider_auth_profiles_path(name).write_text(
            json.dumps(auth_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.provider_models_path(name).write_text(
            json.dumps(models_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def provider_bundle_matches(self, name: str, auth_payload: dict, models_payload: dict) -> bool:
        if not self.provider_exists(name):
            return False
        existing = self.load_provider_bundle(name)
        return existing["auth_profiles"] == auth_payload and existing["models"] == models_payload

    def delete_provider(self, name: str) -> None:
        provider_dir = self.provider_dir(name)
        if provider_dir.exists():
            shutil.rmtree(provider_dir)

    def create_snapshot(self, name: str, datadir: Path, label: str) -> Path:
        return self.create_snapshot_bundle(name, datadir, label)

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

    def restore_snapshot(self, snapshot_dir: Path, datadir: Path) -> None:
        self.restore_snapshot_bundle(snapshot_dir, datadir)

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
