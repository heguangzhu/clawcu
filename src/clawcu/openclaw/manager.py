from __future__ import annotations

import os
from typing import Callable

from clawcu.core.docker import DockerManager
from clawcu.core.storage import StateStore
from clawcu.core.subprocess_utils import CommandError, run_command
from clawcu.core.validation import normalize_version

DEFAULT_OPENCLAW_IMAGE_REPO = "ghcr.io/openclaw/openclaw"
DEFAULT_OPENCLAW_IMAGE_REPO_CN = "ghcr.nju.edu.cn/openclaw/openclaw"
Reporter = Callable[[str], None]


class OpenClawManager:
    def __init__(
        self,
        store: StateStore,
        docker: DockerManager,
        *,
        runner: Callable = run_command,
        image_repo: str | None = None,
        reporter: Reporter | None = None,
    ):
        self.store = store
        self.docker = docker
        self.runner = runner
        configured_image_repo = None
        if hasattr(store, "get_openclaw_image_repo"):
            configured_image_repo = store.get_openclaw_image_repo()
        self.image_repo = image_repo or os.environ.get(
            "CLAWCU_OPENCLAW_IMAGE_REPO",
            configured_image_repo or DEFAULT_OPENCLAW_IMAGE_REPO,
        )
        self.reporter = reporter or (lambda _message: None)

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)

    def official_image_tag(self, version: str) -> str:
        return f"{self.image_repo}:{normalize_version(version)}"

    def pull_official_image(self, version: str) -> str:
        normalized = normalize_version(version)
        official_image = self.official_image_tag(normalized)
        self.reporter(
            f"Step 2/5: Pulling official image {official_image}. This usually takes 10-60 seconds depending on your network."
        )
        self.docker.pull_image(official_image)
        return official_image

    def ensure_image(self, version: str) -> str:
        normalized = normalize_version(version)
        official_image = self.official_image_tag(normalized)
        self.reporter(
            f"Step 2/5: Using OpenClaw image {official_image} for this run. "
            "If it is missing locally, Docker will pull it when the container starts."
        )
        return official_image

    def _is_missing_version_error(self, exc: CommandError) -> bool:
        details = f"{exc.stderr}\n{exc.stdout}".lower()
        missing_markers = (
            "not found",
            "manifest unknown",
            "failed to resolve reference",
            "no such image",
            "remote branch",
            "couldn't find remote ref",
            "not found in upstream origin",
        )
        return any(marker in details for marker in missing_markers)
