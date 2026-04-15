from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from clawcu.core.docker import DockerManager
from clawcu.core.storage import StateStore
from clawcu.core.subprocess_utils import run_command
from clawcu.core.validation import image_tag_for_service, normalize_ref

DEFAULT_HERMES_SOURCE_REPO = "https://github.com/NousResearch/hermes-agent.git"
DEFAULT_HERMES_DOCKERFILE_NAME = "Dockerfile"
OBSERVABLE_HERMES_DOCKERFILE_NAME = "Dockerfile.clawcu"
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
        dockerfile = self.prepare_build_dockerfile(source_dir)
        for attempt in range(1, self.build_attempts + 1):
            self.reporter(
                f"Step 2/5: Building Hermes image {image_tag} from {source_dir} "
                f"(attempt {attempt}/{self.build_attempts}). This may take a while the first time."
            )
            try:
                self.docker.build_image(source_dir, image_tag, dockerfile=dockerfile.name)
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

    def prepare_build_dockerfile(self, source_dir: Path) -> Path:
        source_dockerfile = source_dir / DEFAULT_HERMES_DOCKERFILE_NAME
        observable_dockerfile = source_dir / OBSERVABLE_HERMES_DOCKERFILE_NAME
        original = source_dockerfile.read_text(encoding="utf-8")
        rewritten = self._rewrite_dockerfile_for_observable_builds(original)
        observable_dockerfile.write_text(rewritten, encoding="utf-8")
        if rewritten != original:
            self.reporter(
                "Step 2/5: Preparing a ClawCU build Dockerfile with split Hermes dependency steps for clearer progress."
            )
        return observable_dockerfile

    def _rewrite_dockerfile_for_observable_builds(self, contents: str) -> str:
        pattern = re.compile(
            r"(?ms)^# Install Node dependencies and Playwright as root \(\-\-with-deps needs apt\)\n"
            r"RUN npm install --prefer-offline --no-audit && \\\n"
            r"\s*npx playwright install --with-deps chromium --only-shell && \\\n"
            r"\s*cd /opt/hermes/scripts/whatsapp-bridge && \\\n"
            r"\s*npm install --prefer-offline --no-audit && \\\n"
            r"\s*npm cache clean --force\n"
        )
        replacement = (
            "# Install Node dependencies and Playwright as root (--with-deps needs apt)\n"
            "RUN npm config set registry https://registry.npmmirror.com\n"
            "RUN npm install --prefer-offline --no-audit\n"
            "RUN npx playwright install --with-deps chromium --only-shell\n"
            "RUN cd /opt/hermes/scripts/whatsapp-bridge && npm install --prefer-offline --no-audit\n"
            "RUN npm cache clean --force\n"
        )
        return pattern.sub(replacement, contents, count=1)
