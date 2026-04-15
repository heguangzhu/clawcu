from __future__ import annotations

from pathlib import Path

import pytest

from clawcu.hermes import DEFAULT_HERMES_SOURCE_REPO, HermesManager
from clawcu.hermes.manager import CamoufoxPrefetch
from clawcu.paths import get_paths
from clawcu.storage import StateStore
from tests.support import make_service


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(self, command: list[str], *, cwd=None, capture_output=True, check=True, stream_output=False):
        self.calls.append((command, cwd))
        return type("Completed", (), {"stdout": "", "stderr": "", "returncode": 0})()


class FakeBuildDocker:
    def __init__(self) -> None:
        self.existing_images: set[str] = set()
        self.build_calls: list[tuple[Path, str, str | None]] = []
        self.failures_remaining = 0

    def image_exists(self, image_tag: str) -> bool:
        return image_tag in self.existing_images

    def build_image(self, source_dir: Path, image_tag: str, *, dockerfile: str | None = None) -> None:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("transient docker build failure")
        self.build_calls.append((source_dir, image_tag, dockerfile))


def test_prepare_source_clones_and_checks_out_requested_ref(temp_clawcu_home) -> None:
    store = StateStore(get_paths())
    runner = RecordingRunner()
    docker = FakeBuildDocker()
    manager = HermesManager(store, docker, runner=runner, source_repo=DEFAULT_HERMES_SOURCE_REPO)

    source_dir = manager.prepare_source("v0.9.0")

    assert source_dir == store.source_dir("hermes", "v0.9.0")
    assert runner.calls == [
        (
            [
                "git",
                "clone",
                "--recurse-submodules",
                DEFAULT_HERMES_SOURCE_REPO,
                str(source_dir),
            ],
            None,
        ),
        (["git", "checkout", "v0.9.0"], source_dir),
        (["git", "submodule", "update", "--init", "--recursive"], source_dir),
    ]


def test_ensure_image_builds_from_prepared_source(temp_clawcu_home, monkeypatch) -> None:
    store = StateStore(get_paths())
    docker = FakeBuildDocker()
    messages: list[str] = []
    manager = HermesManager(store, docker, reporter=messages.append)
    source_dir = store.source_dir("hermes", "v0.9.0")
    monkeypatch.setattr(manager, "prepare_source", lambda version: source_dir)
    monkeypatch.setattr(manager, "prepare_build_dockerfile", lambda _source_dir: source_dir / "Dockerfile.clawcu")

    image_tag = manager.ensure_image("v0.9.0")

    assert image_tag == "clawcu/hermes:v0.9.0"
    assert docker.build_calls == [(source_dir, "clawcu/hermes:v0.9.0", "Dockerfile.clawcu")]
    assert any("Building Hermes image clawcu/hermes:v0.9.0" in message for message in messages)


def test_ensure_image_retries_transient_build_failures(temp_clawcu_home, monkeypatch) -> None:
    store = StateStore(get_paths())
    docker = FakeBuildDocker()
    docker.failures_remaining = 1
    messages: list[str] = []
    manager = HermesManager(store, docker, reporter=messages.append)
    source_dir = store.source_dir("hermes", "v0.9.0")
    monkeypatch.setattr(manager, "prepare_source", lambda version: source_dir)
    monkeypatch.setattr(manager, "prepare_build_dockerfile", lambda _source_dir: source_dir / "Dockerfile.clawcu")

    image_tag = manager.ensure_image("v0.9.0")

    assert image_tag == "clawcu/hermes:v0.9.0"
    assert docker.build_calls == [(source_dir, "clawcu/hermes:v0.9.0", "Dockerfile.clawcu")]
    assert any("attempt 1/3" in message for message in messages)
    assert any("attempt 2/3" in message for message in messages)
    assert any("Retrying from the same source checkout" in message for message in messages)


def test_ensure_image_skips_build_when_local_image_exists(temp_clawcu_home) -> None:
    store = StateStore(get_paths())
    docker = FakeBuildDocker()
    docker.existing_images.add("clawcu/hermes:v0.9.0")
    messages: list[str] = []
    manager = HermesManager(store, docker, reporter=messages.append)

    image_tag = manager.ensure_image("v0.9.0")

    assert image_tag == "clawcu/hermes:v0.9.0"
    assert docker.build_calls == []
    assert messages == [
        "Step 2/5: Docker image clawcu/hermes:v0.9.0 already exists locally. Skipping source sync/build."
    ]


def test_prepare_build_dockerfile_splits_heavy_dependency_layer(temp_clawcu_home, monkeypatch) -> None:
    store = StateStore(get_paths())
    docker = FakeBuildDocker()
    manager = HermesManager(store, docker)
    source_dir = store.source_dir("hermes", "v0.9.0")
    source_dir.mkdir(parents=True, exist_ok=True)
    source_dockerfile = source_dir / "Dockerfile"
    source_dockerfile.write_text(
        """FROM debian:13.4

WORKDIR /opt/hermes

# Install Node dependencies and Playwright as root (--with-deps needs apt)
RUN npm install --prefer-offline --no-audit && \\
    npx playwright install --with-deps chromium --only-shell && \\
    cd /opt/hermes/scripts/whatsapp-bridge && \\
    npm install --prefer-offline --no-audit && \\
    npm cache clean --force
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        manager,
        "prepare_camoufox_prefetch",
        lambda _source_dir: CamoufoxPrefetch(
            asset_name="camoufox-135.0.1-beta.24-lin.arm64.zip",
            version="135.0.1",
            release="beta.24",
        ),
    )

    observable_dockerfile = manager.prepare_build_dockerfile(source_dir)

    contents = observable_dockerfile.read_text(encoding="utf-8")
    assert observable_dockerfile == source_dir / "Dockerfile.clawcu"
    assert "RUN npm config set registry https://registry.npmmirror.com\n" in contents
    assert "RUN npm config set progress false && npm config set fund false && npm config set update-notifier false\n" in contents
    assert "RUN npm ci --prefer-offline --no-audit --ignore-scripts\n" in contents
    assert "RUN node node_modules/agent-browser/scripts/postinstall.js\n" in contents
    assert (
        "RUN python3 /opt/hermes/.clawcu-cache/install_camoufox.py "
        "/opt/hermes/.clawcu-cache/camoufox/camoufox-135.0.1-beta.24-lin.arm64.zip "
        "/root/.cache/camoufox 135.0.1 beta.24\n"
    ) in contents
    assert "RUN npx camoufox-js fetch || true\n" in contents
    assert "RUN npx playwright install --with-deps chromium --only-shell\n" in contents
    assert "RUN cd /opt/hermes/scripts/whatsapp-bridge && npm ci --prefer-offline --no-audit --foreground-scripts\n" in contents
    assert "RUN npm cache clean --force\n" in contents


def test_collect_providers_supports_hermes_home(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / ".hermes"
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-hermes\n", encoding="utf-8")

    result = service.collect_providers(path=str(root))
    bundle = store.load_provider_bundle("hermes", "openrouter")

    assert result["saved"] == [f"openrouter (path:{root})"]
    assert bundle["service"] == "hermes"
    assert "config_yaml" in bundle
    assert "OPENROUTER_API_KEY=sk-hermes" in str(bundle["env"])


def test_create_hermes_saves_record_and_writes_native_home(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"

    record = service.create_hermes(
        name="scribe",
        version="v0.9.0",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    assert record.service == "hermes"
    assert record.image_tag == "clawcu/hermes:v0.9.0"
    assert store.load_record("scribe").container_name == "clawcu-hermes-scribe"
    assert (datadir / "config.yaml").exists()


def test_hermes_env_commands_use_datadir_env_file(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"
    service.create_hermes(
        name="scribe",
        version="v0.9.0",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    result = service.set_instance_env("scribe", ["OPENAI_API_KEY=sk-hermes"])

    assert result["path"] == str(datadir / ".env")
    assert (datadir / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-hermes\n"


def test_hermes_token_and_approve_are_unsupported(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"
    service.create_hermes(
        name="scribe",
        version="v0.9.0",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(ValueError, match="not supported"):
        service.token("scribe")
    with pytest.raises(ValueError, match="not supported"):
        service.approve_pairing("scribe")


def test_apply_provider_updates_hermes_config_and_env(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]

    source_root = tmp_path / ".hermes-source"
    source_root.mkdir(parents=True)
    (source_root / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    (source_root / ".env").write_text("OPENROUTER_API_KEY=sk-hermes\n", encoding="utf-8")
    service.collect_providers(path=str(source_root))

    target_root = tmp_path / "hermes-target"
    service.create_hermes(
        name="scribe",
        version="v0.9.0",
        datadir=str(target_root),
        port=8642,
        cpu="1",
        memory="2g",
    )

    result = service.apply_provider(
        "openrouter",
        "scribe",
        primary="openrouter/openai/gpt-5",
        fallbacks=["openrouter/anthropic/claude-sonnet-4.5"],
    )

    assert result["service"] == "hermes"
    assert "OPENROUTER_API_KEY=sk-hermes" in (target_root / ".env").read_text(encoding="utf-8")
    config_yaml = (target_root / "config.yaml").read_text(encoding="utf-8")
    assert "provider: openrouter" in config_yaml
    assert "default: openai/gpt-5" in config_yaml
