from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from clawcu.core.docker import DockerManager
from clawcu.core.storage import StateStore
from clawcu.core.subprocess_utils import run_command
from clawcu.core.validation import image_tag_for_service, normalize_ref

DEFAULT_HERMES_SOURCE_REPO = "https://github.com/NousResearch/hermes-agent.git"
Reporter = Callable[[str], None]


class HermesManager:
    def __init__(
        self,
        store: StateStore,
        docker: DockerManager,
        *,
        runner: Callable = run_command,
        source_repo: str | None = None,
        reporter: Reporter | None = None,
    ):
        self.store = store
        self.docker = docker
        self.runner = runner
        configured_source_repo = None
        if hasattr(store, "get_hermes_source_repo"):
            configured_source_repo = store.get_hermes_source_repo()
        self.source_repo = source_repo or os.environ.get(
            "CLAWCU_HERMES_SOURCE_REPO",
            configured_source_repo or DEFAULT_HERMES_SOURCE_REPO,
        )
        self.reporter = reporter or (lambda _message: None)
        self.build_attempts = 3

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)

    def ensure_image(self, version: str) -> str:
        normalized = normalize_ref(version)
        image_tag = image_tag_for_service("hermes", normalized)
        if self.docker.image_exists(image_tag):
            self.reporter(f"Step 2/5: Docker image {image_tag} already exists locally. Skipping source sync/build.")
            return image_tag
        source_dir = self.prepare_source(normalized)
        for attempt in range(1, self.build_attempts + 1):
            self.reporter(
                f"Step 2/5: Building Hermes image {image_tag} from {source_dir} "
                f"(attempt {attempt}/{self.build_attempts}). This may take a while the first time."
            )
            try:
                self.docker.build_image(source_dir, image_tag)
                break
            except Exception:
                if attempt >= self.build_attempts:
                    raise
                self.reporter(
                    "Hermes image build failed. Retrying from the same source checkout in case the failure was transient."
                )
        return image_tag

    def prepare_source(self, version: str) -> Path:
        normalized = normalize_ref(version)
        source_dir = self.store.source_dir("hermes", normalized)
        if not source_dir.exists():
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            self.reporter(f"Step 1/5: Cloning Hermes source {self.source_repo} at {normalized}.")
            self.runner(["git", "clone", "--recurse-submodules", self.source_repo, str(source_dir)])
        else:
            self.reporter(f"Step 1/5: Refreshing Hermes source checkout for {normalized}.")
            self.runner(["git", "fetch", "--tags", "origin"], cwd=source_dir)
        self.runner(["git", "checkout", normalized], cwd=source_dir)
        self.runner(["git", "submodule", "update", "--init", "--recursive"], cwd=source_dir)
        return source_dir
