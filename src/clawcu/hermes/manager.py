from __future__ import annotations

import os
import re
from typing import Callable

from clawcu.core.docker import DockerManager
from clawcu.core.registry import RemoteTagResult, fetch_remote_tags
from clawcu.core.storage import StateStore
from clawcu.core.subprocess_utils import CommandError, run_command
from clawcu.core.validation import image_tag_for_service, normalize_service_version

DEFAULT_HERMES_IMAGE_REPO = "clawcu/hermes-agent"
Reporter = Callable[[str], None]

# Hermes release tags use semver ("1.2.3", optionally "v1.2.3" or
# "1.2.3-beta.1"). Filter out floating aliases ("latest", "edge"),
# arch/variant suffixes ("1.2.3-amd64"), and commit shas.
_HERMES_RELEASE_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$")


class HermesManager:
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
        if hasattr(store, "get_hermes_image_repo"):
            configured_image_repo = store.get_hermes_image_repo()
        self.image_repo = image_repo or os.environ.get(
            "CLAWCU_HERMES_IMAGE_REPO",
            configured_image_repo or DEFAULT_HERMES_IMAGE_REPO,
        )
        self.reporter = reporter or (lambda _message: None)

    def set_reporter(self, reporter: Reporter | None) -> None:
        self.reporter = reporter or (lambda _message: None)

    def official_image_tag(self, version: str) -> str:
        return f"{self.image_repo}:{normalize_service_version('hermes', version)}"

    def list_remote_versions(
        self,
        *,
        timeout: float | None = None,
        fetcher: Callable[..., RemoteTagResult] | None = None,
    ) -> RemoteTagResult:
        """Fetch the remote tag list for the configured Hermes repo.

        Filters registry output down to semver release tags, stripping a
        leading ``v`` so local and remote buckets compare cleanly.
        """
        fetch = fetcher or fetch_remote_tags
        raw = fetch(self.image_repo, timeout=timeout or 4)
        if not raw.ok:
            return raw
        filtered = sorted(
            {
                tag.lstrip("v")
                for tag in (raw.tags or [])
                if _HERMES_RELEASE_TAG.match(tag)
            }
        )
        return RemoteTagResult(
            repo=raw.repo,
            registry=raw.registry,
            tags=filtered,
            pages_fetched=raw.pages_fetched,
            extras={"raw_tag_count": len(raw.tags or [])},
        )

    def pull_official_image(self, version: str) -> str:
        normalized = normalize_service_version("hermes", version)
        official_image = self.official_image_tag(normalized)
        local_image = image_tag_for_service("hermes", normalized)
        self.reporter(
            f"Step 2/5: Pulling Hermes image {official_image}. This usually takes 10-60 seconds depending on your network."
        )
        self.docker.pull_image(official_image)
        if official_image != local_image:
            self.reporter(f"Step 2/5: Tagging pulled image as {local_image} for local ClawCU management.")
            self.docker.tag_image(official_image, local_image)
        return local_image

    def ensure_image(self, version: str) -> str:
        normalized = normalize_service_version("hermes", version)
        image_tag = image_tag_for_service("hermes", normalized)
        if not self.docker.image_exists(image_tag):
            try:
                return self.pull_official_image(normalized)
            except CommandError as exc:
                if self._is_missing_version_error(exc):
                    raise RuntimeError(
                        f"Hermes version {normalized} was not found in the configured image registry {self.image_repo}."
                    ) from exc
                raise RuntimeError(
                    f"Failed to prepare Hermes {normalized} from the configured image registry {self.image_repo}: {exc}"
                ) from exc
        self.reporter(f"Step 2/5: Docker image {image_tag} already exists locally. Skipping pull.")
        return image_tag

    def _is_missing_version_error(self, exc: CommandError) -> bool:
        details = f"{exc.stderr}\n{exc.stdout}".lower()
        missing_markers = (
            "not found",
            "manifest unknown",
            "failed to resolve reference",
            "no such image",
            "pull access denied",
        )
        return any(marker in details for marker in missing_markers)
