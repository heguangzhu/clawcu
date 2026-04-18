from __future__ import annotations

import stat
from pathlib import Path

import pytest

from clawcu.hermes import DEFAULT_HERMES_IMAGE_REPO, HermesManager
from clawcu.paths import get_paths
from clawcu.storage import StateStore
from clawcu.validation import build_instance_record
from tests.support import make_service


class FakePullDocker:
    def __init__(self) -> None:
        self.existing_images: set[str] = set()
        self.pull_calls: list[str] = []
        self.tag_calls: list[tuple[str, str]] = []

    def image_exists(self, image_tag: str) -> bool:
        return image_tag in self.existing_images

    def pull_image(self, image_tag: str) -> None:
        self.pull_calls.append(image_tag)

    def tag_image(self, source_image: str, target_image: str) -> None:
        self.tag_calls.append((source_image, target_image))


def test_pull_official_image_tags_custom_repo_into_local_managed_name(temp_clawcu_home) -> None:
    store = StateStore(get_paths())
    docker = FakePullDocker()
    messages: list[str] = []
    manager = HermesManager(
        store,
        docker,
        image_repo="registry.example.com/hermes-agent",
        reporter=messages.append,
    )

    image_tag = manager.pull_official_image("v2026.4.8")

    assert image_tag == "clawcu/hermes-agent:v2026.4.8"
    assert docker.pull_calls == ["registry.example.com/hermes-agent:v2026.4.8"]
    assert docker.tag_calls == [
        ("registry.example.com/hermes-agent:v2026.4.8", "clawcu/hermes-agent:v2026.4.8")
    ]
    assert any("Pulling Hermes image registry.example.com/hermes-agent:v2026.4.8" in message for message in messages)


def test_ensure_image_pulls_when_local_image_is_missing(temp_clawcu_home) -> None:
    store = StateStore(get_paths())
    docker = FakePullDocker()
    messages: list[str] = []
    manager = HermesManager(store, docker, reporter=messages.append)

    image_tag = manager.ensure_image("v2026.4.8")

    assert image_tag == "clawcu/hermes-agent:v2026.4.8"
    assert docker.pull_calls == ["clawcu/hermes-agent:v2026.4.8"]
    assert docker.tag_calls == []
    assert any("Pulling Hermes image clawcu/hermes-agent:v2026.4.8" in message for message in messages)


def test_ensure_image_accepts_numeric_date_version_and_maps_to_v_tag(temp_clawcu_home) -> None:
    store = StateStore(get_paths())
    docker = FakePullDocker()
    messages: list[str] = []
    manager = HermesManager(store, docker, reporter=messages.append)

    image_tag = manager.ensure_image("2026.4.8")

    assert image_tag == "clawcu/hermes-agent:v2026.4.8"
    assert docker.pull_calls == ["clawcu/hermes-agent:v2026.4.8"]
    assert any("Pulling Hermes image clawcu/hermes-agent:v2026.4.8" in message for message in messages)


def test_ensure_image_skips_pull_when_local_image_exists(temp_clawcu_home) -> None:
    store = StateStore(get_paths())
    docker = FakePullDocker()
    docker.existing_images.add("clawcu/hermes-agent:v2026.4.8")
    messages: list[str] = []
    manager = HermesManager(store, docker, reporter=messages.append)

    image_tag = manager.ensure_image("v2026.4.8")

    assert image_tag == "clawcu/hermes-agent:v2026.4.8"
    assert docker.pull_calls == []
    assert messages == [
        "Step 2/5: Docker image clawcu/hermes-agent:v2026.4.8 already exists locally. Skipping pull."
    ]


def test_collect_providers_supports_hermes_home(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / ".hermes"
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "OPENROUTER_API_KEY=sk-hermes\nAPI_SERVER_KEY=server-secret\n",
        encoding="utf-8",
    )

    result = service.collect_providers(path=str(root))
    bundle = store.load_provider_bundle("hermes", "openrouter")

    assert result["saved"] == [f"openrouter (path:{root})"]
    assert bundle["service"] == "hermes"
    assert "config_yaml" in bundle
    assert "OPENROUTER_API_KEY=sk-hermes" in str(bundle["env"])
    assert "API_SERVER_KEY" not in str(bundle["env"])


def test_create_hermes_saves_record_and_writes_native_home(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"

    record = service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    assert record.service == "hermes"
    assert record.version == "v2026.4.8"
    assert record.image_tag == "clawcu/hermes-agent:v2026.4.8"
    assert record.dashboard_port == 9129
    assert store.load_record("scribe").container_name == "clawcu-hermes-scribe"
    config_path = datadir / "config.yaml"
    assert config_path.exists()
    assert "backend: local" in config_path.read_text(encoding="utf-8")
    env_path = datadir / ".env"
    assert env_path.exists()
    assert "API_SERVER_KEY=" in env_path.read_text(encoding="utf-8")
    assert (datadir / ".hermes").is_dir()
    assert stat.S_IMODE(datadir.stat().st_mode) == 0o777
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o666
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o666


def test_create_hermes_defaults_datadir_and_port(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port in {8652, 9129}  # type: ignore[method-assign]
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]

    record = service.create_hermes(
        name="scribe",
        version="2026.4.8",
        cpu="1",
        memory="2g",
    )

    assert record.datadir.endswith("/.clawcu/scribe")
    assert record.port == 8652
    assert record.dashboard_port == 9129
    assert record.auth_mode == "native"


def test_hermes_run_spec_respects_image_entrypoint(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    adapter = service.adapters["hermes"]
    spec = adapter.build_spec(
        service,
        name="scribe",
        version="2026.4.8",
        datadir=str(tmp_path / "hermes-home"),
        port=8642,
        cpu="1",
        memory="2g",
    )
    instance = build_instance_record(spec, status="creating", history=[])
    adapter.configure_before_run(service, instance)

    run_spec = adapter.run_spec(service, instance)

    assert run_spec.command == ["gateway", "run"]
    assert run_spec.extra_env["API_SERVER_HOST"] == "0.0.0.0"
    assert run_spec.extra_env["API_SERVER_KEY"]
    assert run_spec.additional_ports == [(9129, 9119)]
    assert run_spec.additional_mounts == [
        (str(tmp_path / "hermes-home" / ".hermes"), "/root/.hermes")
    ]


def test_hermes_access_info_points_to_dashboard(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    adapter = service.adapters["hermes"]
    spec = adapter.build_spec(
        service,
        name="scribe",
        version="2026.4.8",
        datadir=str(tmp_path / "hermes-home"),
        port=8642,
        cpu="1",
        memory="2g",
    )
    instance = build_instance_record(spec, status="running", history=[])

    access = adapter.access_info(service, instance)

    assert access.base_url == "http://127.0.0.1:9129/"
    assert access.readiness_label == "dashboard"
    assert access.auth_hint == "Hermes dashboard (use `clawcu tui <instance>` for chat, API server stays on /health)"
    assert adapter.display_port(service, instance) == 9129


def test_list_instance_summaries_show_hermes_dashboard_port_and_access(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"

    service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    [summary] = service.list_instance_summaries()

    assert summary["service"] == "hermes"
    assert summary["port"] == 9129
    assert summary["access_url"] == "http://127.0.0.1:9129/"


def test_local_hermes_summary_uses_dashboard_port_and_access(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    adapter = service.adapters["hermes"]
    root = tmp_path / ".hermes"
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    service._local_hermes_home = lambda: root  # type: ignore[method-assign]
    adapter._local_version = lambda _service: "v0.9.0 (2026.4.13)"  # type: ignore[method-assign]

    [summary] = adapter.local_instance_summaries(service)

    assert summary["port"] == 9119
    assert summary["access_url"] == "http://127.0.0.1:9119/"


def test_hermes_env_commands_use_datadir_env_file(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"
    service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    result = service.set_instance_env("scribe", ["OPENAI_API_KEY=sk-hermes"])

    assert result["path"] == str(datadir / ".env")
    env_text = (datadir / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-hermes\n" in env_text
    assert "API_SERVER_KEY=" in env_text


def test_configure_hermes_runs_setup_command(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"

    service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    service.configure_instance("scribe", extra_args=["--help"])

    container_name, command, options = docker.interactive_exec_commands[-1]
    assert container_name == "clawcu-hermes-scribe"
    assert command == ["hermes", "setup", "--help"]
    assert "API_SERVER_KEY" in options["env"]
    assert options["env"]["PATH"].startswith("/opt/hermes/.venv/bin:/opt/hermes:")


def test_exec_instance_rewrites_hermes_cli_to_path_resolved_command(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"

    service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    service.exec_instance("scribe", ["hermes", "version"])

    container_name, command, options = docker.interactive_exec_commands[-1]
    assert container_name == "clawcu-hermes-scribe"
    assert command == ["hermes", "version"]
    assert "API_SERVER_KEY" in options["env"]
    assert options["env"]["PATH"].startswith("/opt/hermes/.venv/bin:/opt/hermes:")


def test_tui_hermes_uses_path_resolved_hermes_command(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"

    service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
    )

    service.tui_instance("scribe")

    container_name, command, options = docker.interactive_exec_commands[-1]
    assert container_name == "clawcu-hermes-scribe"
    assert command == ["hermes", "chat"]
    assert "API_SERVER_KEY" in options["env"]
    assert options["env"]["PATH"].startswith("/opt/hermes/.venv/bin:/opt/hermes:")


def test_hermes_token_and_approve_are_unsupported(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = tmp_path / "hermes-home"
    service.create_hermes(
        name="scribe",
        version="2026.4.8",
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
        version="2026.4.8",
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


def test_default_hermes_image_repo_constant() -> None:
    assert DEFAULT_HERMES_IMAGE_REPO == "clawcu/hermes-agent"


def test_hermes_list_remote_versions_filters_semver_tags() -> None:
    from clawcu.core.registry import RemoteTagResult

    store = type("NoStore", (), {})()
    docker = type("NoDocker", (), {})()
    manager = HermesManager(store=store, docker=docker, image_repo="clawcu/hermes-agent")  # type: ignore[arg-type]

    def fake_fetcher(repo: str, *, timeout: float = 0) -> RemoteTagResult:
        return RemoteTagResult(
            repo=repo,
            registry="registry-1.docker.io",
            tags=[
                "1.0.0",
                "v1.1.0",
                "1.2.0-beta.1",
                "latest",
                "edge",
                "sha-deadbeef",
            ],
        )

    result = manager.list_remote_versions(fetcher=fake_fetcher)
    assert result.ok
    assert result.tags is not None
    assert "1.0.0" in result.tags
    assert "1.1.0" in result.tags
    assert "1.2.0-beta.1" in result.tags
    assert "latest" not in result.tags
    assert "edge" not in result.tags


def test_hermes_list_remote_versions_preserves_error_passthrough() -> None:
    from clawcu.core.registry import RemoteTagResult

    store = type("NoStore", (), {})()
    docker = type("NoDocker", (), {})()
    manager = HermesManager(store=store, docker=docker, image_repo="clawcu/hermes-agent")  # type: ignore[arg-type]

    def failing(repo: str, *, timeout: float = 0) -> RemoteTagResult:
        return RemoteTagResult(
            repo=repo,
            registry="registry-1.docker.io",
            error="unauthorized (private repo or token expired)",
        )

    result = manager.list_remote_versions(fetcher=failing)
    assert not result.ok
    assert result.error is not None
    assert "unauthorized" in result.error
