from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClawCUPaths:
    home: Path
    instances_dir: Path
    providers_dir: Path
    sources_dir: Path
    logs_dir: Path
    snapshots_dir: Path


def get_paths() -> ClawCUPaths:
    home = Path(os.environ.get("CLAWCU_HOME", "~/.clawcu")).expanduser().resolve()
    paths = ClawCUPaths(
        home=home,
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
