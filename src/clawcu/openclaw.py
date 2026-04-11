from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from clawcu.docker import DockerManager
from clawcu.storage import StateStore
from clawcu.subprocess_utils import CommandError, run_command
from clawcu.validation import image_tag_for_version, normalize_version, upstream_ref_for_version

DEFAULT_REPO_URL = "https://github.com/openclaw/openclaw.git"
DEFAULT_IMAGE_REPO = "ghcr.io/openclaw/openclaw"
Reporter = Callable[[str], None]


class OpenClawManager:
    def __init__(
        self,
        store: StateStore,
        docker: DockerManager,
        *,
        runner: Callable = run_command,
        repo_url: str | None = None,
        image_repo: str | None = None,
        docker_target: str = "slim",
        reporter: Reporter | None = None,
    ):
        self.store = store
        self.docker = docker
        self.runner = runner
        self.repo_url = repo_url or os.environ.get("CLAWCU_OPENCLAW_REPO", DEFAULT_REPO_URL)
        self.image_repo = image_repo or os.environ.get("CLAWCU_OPENCLAW_IMAGE_REPO", DEFAULT_IMAGE_REPO)
        self.docker_target = docker_target
        self.reporter = reporter or (lambda _message: None)

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)

    def source_dir_for_version(self, version: str) -> Path:
        return self.store.source_dir("openclaw", normalize_version(version))

    def pull_source(self, version: str) -> Path:
        normalized = normalize_version(version)
        source_dir = self.source_dir_for_version(normalized)
        if not (source_dir / ".git").exists():
            self.reporter(
                f"Step 2/5: Cloning OpenClaw {upstream_ref_for_version(normalized)} source code. This usually takes 10-30 seconds."
            )
            self.runner(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    upstream_ref_for_version(normalized),
                    self.repo_url,
                    str(source_dir),
                ],
            )
        else:
            self.reporter(
                f"Step 2/5: Reusing cached OpenClaw {upstream_ref_for_version(normalized)} source code."
            )
        return source_dir

    def build_image(self, version: str) -> str:
        normalized = normalize_version(version)
        source_dir = self.pull_source(normalized)
        image_tag = image_tag_for_version(normalized)
        self.reporter(
            f"Step 3/5: Building Docker image {image_tag}. First build can take several minutes; Docker output follows."
        )
        self.docker.build_image(source_dir, image_tag, preferred_variant=self.docker_target)
        return image_tag

    def official_image_tag(self, version: str) -> str:
        return f"{self.image_repo}:{normalize_version(version)}"

    def pull_official_image(self, version: str) -> str:
        normalized = normalize_version(version)
        official_image = self.official_image_tag(normalized)
        local_image = image_tag_for_version(normalized)
        self.reporter(
            f"Step 2/5: Pulling official image {official_image}. This usually takes 10-60 seconds depending on your network."
        )
        self.docker.pull_image(official_image)
        if official_image != local_image:
            self.reporter(f"Step 2/5: Tagging official image as {local_image} for local ClawCU management.")
            self.docker.tag_image(official_image, local_image)
        return local_image

    def ensure_image(self, version: str) -> str:
        normalized = normalize_version(version)
        image_tag = image_tag_for_version(normalized)
        if not self.docker.image_exists(image_tag):
            try:
                return self.pull_official_image(normalized)
            except CommandError as exc:
                if self._is_missing_version_error(exc):
                    self.reporter(
                        f"Step 2/5: Official image for OpenClaw {normalized} was not found. Trying the upstream git tag {upstream_ref_for_version(normalized)} instead."
                    )
                else:
                    self.reporter(
                        f"Step 2/5: Official image pull failed ({exc.returncode}). Falling back to local source build, which can take several minutes."
                    )
                try:
                    return self.build_image(normalized)
                except CommandError as build_exc:
                    if self._is_missing_version_error(build_exc):
                        raise RuntimeError(
                            f"OpenClaw version {normalized} was not found in the official image registry or upstream git tag {upstream_ref_for_version(normalized)}."
                        ) from build_exc
                    raise RuntimeError(
                        f"Failed to prepare OpenClaw {normalized}. Official image pull failed, and the local source fallback also failed: {build_exc}"
                    ) from build_exc
        self.reporter(f"Step 2/5: Docker image {image_tag} already exists locally. Skipping clone/build.")
        return image_tag

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
