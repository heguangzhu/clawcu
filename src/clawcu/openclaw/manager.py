from __future__ import annotations

import os
import re
from typing import Callable

from clawcu.core.docker import DockerManager
from clawcu.core.registry import RemoteTagResult, fetch_remote_tags, semver_sort_key
from clawcu.core.storage import StateStore
from clawcu.core.subprocess_utils import CommandError, run_command
from clawcu.core.validation import normalize_version

# OpenClaw tags are year-dot-month-dot-patch ("2026.4.2"), optionally
# prefixed with "v" and optionally followed by a pre-release segment
# such as "-beta.1". Per-platform ("-amd64", "-arm64") and image-variant
# ("-slim", "-alpine") tags are excluded — they duplicate the canonical
# multi-arch manifest list and would just clutter --list-versions.
_OPENCLAW_RELEASE_TAG = re.compile(
    r"^v?\d{4}\.\d{1,2}\.\d+"
    r"(?:-(?:alpha|beta|rc|preview|pre|dev)(?:\.\d+)?)?$"
)

# Token-based denylist for things that should never appear as a
# "canonical release" tag even if they sneak past the regex.
_OPENCLAW_VARIANT_TOKENS = (
    "amd64",
    "arm64",
    "armhf",
    "armv7",
    "386",
    "ppc64le",
    "s390x",
    "slim",
    "alpine",
    "debug",
    "nightly",
)


def _is_openclaw_release_tag(tag: str) -> bool:
    if not _OPENCLAW_RELEASE_TAG.match(tag):
        return False
    segments = tag.lstrip("v").lower().split("-")
    # First segment is the "YYYY.M.P" number; remaining segments must
    # not be platform/variant tokens.
    for segment in segments[1:]:
        head = segment.split(".", 1)[0]
        if head in _OPENCLAW_VARIANT_TOKENS:
            return False
    return True

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

    def list_remote_versions(
        self,
        *,
        timeout: float | None = None,
        fetcher: Callable[..., RemoteTagResult] | None = None,
    ) -> RemoteTagResult:
        """Fetch the remote tag list for the configured OpenClaw repo.

        The raw registry output contains build artifacts we do not want
        to surface (``latest``, ``main``, commit shas). This wrapper
        filters tags down to OpenClaw's semantic release pattern —
        ``YYYY.M.P`` optionally prefixed with ``v`` — and returns a new
        ``RemoteTagResult`` with the filtered list. Errors are preserved
        on the result untouched.
        """
        fetch = fetcher or fetch_remote_tags
        raw = fetch(self.image_repo, timeout=timeout or 4)
        if not raw.ok:
            return raw
        filtered = sorted(
            {
                tag.lstrip("v")
                for tag in (raw.tags or [])
                if _is_openclaw_release_tag(tag)
            },
            key=semver_sort_key,
        )
        return RemoteTagResult(
            repo=raw.repo,
            registry=raw.registry,
            tags=filtered,
            pages_fetched=raw.pages_fetched,
            extras={"raw_tag_count": len(raw.tags or [])},
        )

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
