from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CLAWCU_HOME = Path("~/.clawcu").expanduser().resolve()


@dataclass(frozen=True)
class ClawCUPaths:
    home: Path
    config_path: Path
    instances_dir: Path
    providers_dir: Path
    sources_dir: Path
    logs_dir: Path
    snapshots_dir: Path


def bootstrap_config_path() -> Path:
    return (Path.home() / ".config" / "clawcu" / "bootstrap.json").expanduser().resolve()


def _load_bootstrap_home() -> Path | None:
    path = bootstrap_config_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    value = payload.get("clawcu_home")
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return None


def resolve_clawcu_home() -> Path:
    explicit = os.environ.get("CLAWCU_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _load_bootstrap_home() or DEFAULT_CLAWCU_HOME


def build_paths(home: Path) -> ClawCUPaths:
    home = home.expanduser().resolve()
    paths = ClawCUPaths(
        home=home,
        config_path=home / "config.json",
        instances_dir=home / "instances",
        providers_dir=home / "providers",
        sources_dir=home / "sources",
        logs_dir=home / "logs",
        snapshots_dir=home / "snapshots",
    )
    for path in (
        paths.home,
        paths.instances_dir,
        paths.providers_dir,
        paths.sources_dir,
        paths.logs_dir,
        paths.snapshots_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def get_paths() -> ClawCUPaths:
    paths = build_paths(resolve_clawcu_home())
    return paths
