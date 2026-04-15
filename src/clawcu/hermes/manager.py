from __future__ import annotations

import io
import json
import os
import re
import tarfile
import urllib.request
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CamoufoxPrefetch:
    asset_name: str
    version: str
    release: str
    context_dir: Path


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
        camoufox_prefetch = self.prepare_camoufox_prefetch(source_dir)
        dockerfile = self.prepare_build_dockerfile(source_dir, camoufox_prefetch=camoufox_prefetch)
        build_contexts: dict[str, str | Path] | None = None
        if camoufox_prefetch:
            build_contexts = {"clawcu_cache": camoufox_prefetch.context_dir}
        for attempt in range(1, self.build_attempts + 1):
            self.reporter(
                f"Step 2/5: Building Hermes image {image_tag} from {source_dir} "
                f"(attempt {attempt}/{self.build_attempts}). This may take a while the first time."
            )
            try:
                self.docker.build_image(
                    source_dir,
                    image_tag,
                    dockerfile=dockerfile.name,
                    build_contexts=build_contexts,
                )
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

    def prepare_build_dockerfile(
        self,
        source_dir: Path,
        *,
        camoufox_prefetch: CamoufoxPrefetch | None = None,
    ) -> Path:
        source_dockerfile = source_dir / DEFAULT_HERMES_DOCKERFILE_NAME
        observable_dockerfile = source_dir / OBSERVABLE_HERMES_DOCKERFILE_NAME
        original = source_dockerfile.read_text(encoding="utf-8")
        rewritten = self._rewrite_dockerfile_for_observable_builds(original, camoufox_prefetch=camoufox_prefetch)
        observable_dockerfile.write_text(rewritten, encoding="utf-8")
        if rewritten != original:
            self.reporter(
                "Step 2/5: Preparing a ClawCU build Dockerfile with split Hermes dependency steps for clearer progress."
            )
        return observable_dockerfile

    def _rewrite_dockerfile_for_observable_builds(
        self,
        contents: str,
        *,
        camoufox_prefetch: CamoufoxPrefetch | None = None,
    ) -> str:
        camoufox_install_step = "RUN npx camoufox-js fetch || true\n"
        if camoufox_prefetch:
            camoufox_install_step = (
                "COPY --from=clawcu_cache install_camoufox.py /opt/clawcu-cache/install_camoufox.py\n"
                f"COPY --from=clawcu_cache {camoufox_prefetch.asset_name} /opt/clawcu-cache/camoufox.zip\n"
                "RUN python3 /opt/clawcu-cache/install_camoufox.py "
                "/opt/clawcu-cache/camoufox.zip "
                f"/root/.cache/camoufox {camoufox_prefetch.version} {camoufox_prefetch.release}\n"
                "RUN npx camoufox-js fetch || true\n"
            )
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
            "RUN npm config set progress false && npm config set fund false && npm config set update-notifier false\n"
            "RUN npm ci --prefer-offline --no-audit --ignore-scripts\n"
            "RUN node node_modules/agent-browser/scripts/postinstall.js\n"
            f"{camoufox_install_step}"
            "RUN npx playwright install --with-deps chromium --only-shell\n"
            "RUN cd /opt/hermes/scripts/whatsapp-bridge && npm ci --prefer-offline --no-audit --foreground-scripts\n"
            "RUN npm cache clean --force\n"
        )
        return pattern.sub(replacement, contents, count=1)

    def prepare_camoufox_prefetch(self, source_dir: Path) -> CamoufoxPrefetch | None:
        cache_dir = self.store.paths.home / "cache" / "hermes" / "camoufox" / normalize_ref(source_dir.name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        script_path = cache_dir / "install_camoufox.py"
        metadata_path = cache_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                asset_name = metadata["asset_name"]
                asset_path = cache_dir / asset_name
                if asset_path.exists():
                    self._write_camoufox_install_script(script_path)
                    return CamoufoxPrefetch(
                        asset_name=asset_name,
                        version=metadata["version"],
                        release=metadata["release"],
                        context_dir=cache_dir,
                    )
            except Exception:
                pass

        try:
            asset = self._select_camoufox_asset(source_dir)
        except Exception as exc:
            self.reporter(
                f"Step 2/5: Could not prefetch Camoufox asset on the host ({exc}). Falling back to in-container fetch."
            )
            return None

        asset_name = asset["asset_name"]
        asset_path = cache_dir / asset_name
        if not asset_path.exists():
            self.reporter(
                f"Step 2/5: Prefetching Camoufox browser asset {asset_name} on the host to avoid slow in-container GitHub downloads."
            )
            urllib.request.urlretrieve(asset["url"], asset_path)
        metadata_path.write_text(
            json.dumps(
                {
                    "asset_name": asset_name,
                    "version": asset["version"],
                    "release": asset["release"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self._write_camoufox_install_script(script_path)
        return CamoufoxPrefetch(
            asset_name=asset_name,
            version=asset["version"],
            release=asset["release"],
            context_dir=cache_dir,
        )

    def _write_camoufox_install_script(self, script_path: Path) -> None:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (
                "import json\n"
                "import os\n"
                "import shutil\n"
                "import stat\n"
                "import sys\n"
                "import zipfile\n"
                "\n"
                "zip_path, install_dir, version, release = sys.argv[1:5]\n"
                "if os.path.exists(install_dir):\n"
                "    shutil.rmtree(install_dir)\n"
                "os.makedirs(install_dir, exist_ok=True)\n"
                "with zipfile.ZipFile(zip_path) as zf:\n"
                "    zf.extractall(install_dir)\n"
                "with open(os.path.join(install_dir, 'version.json'), 'w', encoding='utf-8') as fh:\n"
                "    json.dump({'version': version, 'release': release}, fh)\n"
                "for root, dirs, files in os.walk(install_dir):\n"
                "    os.chmod(root, 0o755)\n"
                "    for name in dirs:\n"
                "        os.chmod(os.path.join(root, name), 0o755)\n"
                "    for name in files:\n"
                "        path = os.path.join(root, name)\n"
                "        mode = os.stat(path).st_mode\n"
                "        os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)\n"
            ),
            encoding="utf-8",
        )

    def _select_camoufox_asset(self, source_dir: Path) -> dict[str, str]:
        camoufox_js_version = self._camoufox_js_version(source_dir)
        if not camoufox_js_version:
            raise ValueError("camoufox-js version was not found in package-lock.json")
        min_release, max_release = self._camoufox_release_constraints(camoufox_js_version)
        arch = self._docker_target_arch()
        pattern = re.compile(rf"^camoufox-(.+)-(.+)-lin\.{re.escape(arch)}\.zip$")
        releases = self._fetch_json("https://api.github.com/repos/daijro/camoufox/releases")
        for release in releases:
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                match = pattern.match(name)
                if not match:
                    continue
                version, release_name = match.groups()
                if not self._release_is_supported(release_name, min_release, max_release):
                    continue
                return {
                    "asset_name": name,
                    "version": version,
                    "release": release_name,
                    "url": asset["browser_download_url"],
                }
        raise ValueError(f"no supported Camoufox asset found for linux/{arch}")

    def _camoufox_js_version(self, source_dir: Path) -> str | None:
        lockfile = source_dir / "package-lock.json"
        if not lockfile.exists():
            return None
        payload = json.loads(lockfile.read_text(encoding="utf-8"))
        package = payload.get("packages", {}).get("node_modules/camoufox-js", {})
        version = package.get("version")
        return str(version) if version else None

    def _camoufox_release_constraints(self, camoufox_js_version: str) -> tuple[str, str]:
        tarball_url = f"https://registry.npmjs.org/camoufox-js/-/camoufox-js-{camoufox_js_version}.tgz"
        with urllib.request.urlopen(tarball_url) as response:
            contents = response.read()
        with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
            member = archive.extractfile("package/dist/__version__.js")
            if member is None:
                raise ValueError("camoufox-js version constraints were not found")
            version_js = member.read().decode("utf-8")
        min_match = re.search(r'MIN_VERSION = "([^"]+)"', version_js)
        max_match = re.search(r'MAX_VERSION = "([^"]+)"', version_js)
        if not min_match or not max_match:
            raise ValueError("camoufox-js version constraints were not found")
        return min_match.group(1), max_match.group(1)

    def _docker_target_arch(self) -> str:
        machine = os.uname().machine.lower()
        if machine in {"arm64", "aarch64"}:
            return "arm64"
        if machine in {"x86_64", "amd64"}:
            return "x86_64"
        if machine in {"i386", "i686"}:
            return "i686"
        raise ValueError(f"unsupported Docker target architecture: {machine}")

    def _fetch_json(self, url: str) -> object:
        with urllib.request.urlopen(url) as response:
            return json.load(response)

    def _release_is_supported(self, release: str, minimum: str, maximum: str) -> bool:
        return self._release_less_than(minimum, release) and self._release_less_than(release, maximum)

    def _release_less_than(self, left: str, right: str) -> bool:
        for left_value, right_value in zip(self._sorted_release(left), self._sorted_release(right)):
            if left_value < right_value:
                return True
            if left_value > right_value:
                return False
        return False

    def _sorted_release(self, release: str) -> list[int]:
        parts: list[int] = []
        for component in release.split("."):
            if component.isdigit():
                parts.append(int(component))
            else:
                parts.append(ord(component[0]) - 1024)
        while len(parts) < 5:
            parts.append(0)
        return parts
