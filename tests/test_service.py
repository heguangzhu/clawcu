from __future__ import annotations

import json
import socket
import stat
import urllib.request
from pathlib import Path

import pytest

from clawcu.models import InstanceRecord
from clawcu.paths import bootstrap_config_path
from clawcu.hermes import DEFAULT_HERMES_IMAGE_REPO
from clawcu.openclaw import DEFAULT_OPENCLAW_IMAGE_REPO, DEFAULT_OPENCLAW_IMAGE_REPO_CN
from clawcu.subprocess_utils import CommandError
from tests.support import make_service, write_provider_source, write_root_provider_source


def test_check_setup_reports_missing_docker_cli(temp_clawcu_home, monkeypatch) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    monkeypatch.setattr("clawcu.service.shutil.which", lambda _name: None)

    checks = service.check_setup()

    assert checks == [
        {
            "name": "docker_cli",
            "status": "fail",
            "ok": False,
            "summary": "Docker CLI is not installed.",
            "hint": "Install Docker Desktop or another Docker distribution, then rerun `clawcu setup`.",
        }
    ]


def test_check_setup_reports_running_docker_daemon(temp_clawcu_home, monkeypatch) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    monkeypatch.setattr("clawcu.service.shutil.which", lambda _name: "/usr/local/bin/docker")
    service.runner = lambda command: type("Completed", (), {"stdout": '"29.3.1"\n'})()

    checks = service.check_setup()

    assert checks == [
        {
            "name": "docker_cli",
            "status": "ok",
            "ok": True,
            "summary": "Docker CLI is installed at /usr/local/bin/docker.",
            "hint": "",
        },
        {
            "name": "docker_daemon",
            "status": "ok",
            "ok": True,
            "summary": "Docker daemon is running (server 29.3.1).",
            "hint": "",
        },
        {
            "name": "clawcu_home",
            "status": "ok",
            "ok": True,
            "summary": f"ClawCU home directory is ready at {store.paths.home}.",
            "hint": "",
        },
        {
            "name": "clawcu_runtime_dirs",
            "status": "ok",
            "ok": True,
            "summary": (
                "ClawCU runtime directories are ready: "
                f"{store.paths.instances_dir}, {store.paths.providers_dir}, {store.paths.sources_dir}, {store.paths.logs_dir}, {store.paths.snapshots_dir}."
            ),
            "hint": "",
        },
        {
            "name": "openclaw_image_repo",
            "status": "ok",
            "ok": True,
            "summary": "OpenClaw image repo is configured as ghcr.io/openclaw/openclaw.",
            "hint": "",
        },
        {
            "name": "hermes_image_repo",
            "status": "ok",
            "ok": True,
            "summary": "Hermes image repo is configured as clawcu/hermes-agent.",
            "hint": "",
        },
    ]


def test_is_port_available_rejects_port_bound_on_any_interface(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        assert service._is_port_available(port) is False


def test_check_setup_reports_unreachable_docker_daemon(temp_clawcu_home, monkeypatch) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    monkeypatch.setattr("clawcu.service.shutil.which", lambda _name: "/usr/local/bin/docker")

    def failing_runner(command):
        raise CommandError(command, 1, "", "Cannot connect to the Docker daemon")

    service.runner = failing_runner

    checks = service.check_setup()

    assert checks[0]["ok"] is True
    assert checks[1]["ok"] is False
    assert checks[1]["status"] == "fail"
    assert checks[1]["summary"] == "Docker daemon is not reachable."
    assert "docker version" in str(checks[1]["hint"])


def test_set_openclaw_image_repo_persists_global_config(temp_clawcu_home) -> None:
    service, _, _, store = make_service(temp_clawcu_home)

    saved = service.set_openclaw_image_repo("registry.example.com/openclaw/openclaw")

    assert saved == "registry.example.com/openclaw/openclaw"
    assert store.get_openclaw_image_repo() == "registry.example.com/openclaw/openclaw"
    assert json.loads(store.paths.config_path.read_text(encoding="utf-8")) == {
        "openclaw_image_repo": "registry.example.com/openclaw/openclaw"
    }


def test_set_hermes_image_repo_persists_global_config(temp_clawcu_home) -> None:
    service, _, _, store = make_service(temp_clawcu_home)

    saved = service.set_hermes_image_repo("registry.example.com/hermes-agent")

    assert saved == "registry.example.com/hermes-agent"
    assert store.get_hermes_image_repo() == "registry.example.com/hermes-agent"
    assert service.hermes.image_repo == "registry.example.com/hermes-agent"
    assert json.loads(store.paths.config_path.read_text(encoding="utf-8")) == {
        "hermes_image_repo": "registry.example.com/hermes-agent"
    }


def test_get_hermes_image_repo_falls_back_to_default(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)

    assert service.get_hermes_image_repo() == DEFAULT_HERMES_IMAGE_REPO


def test_suggest_openclaw_image_repo_uses_china_mirror_when_ip_is_in_china(temp_clawcu_home, monkeypatch) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)

    monkeypatch.setattr(service, "_detect_public_country_code", lambda: "CN")

    assert service.suggest_openclaw_image_repo() == DEFAULT_OPENCLAW_IMAGE_REPO_CN


def test_suggest_openclaw_image_repo_falls_back_to_global_default(temp_clawcu_home, monkeypatch) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)

    monkeypatch.setattr(service, "_detect_public_country_code", lambda: None)

    assert service.suggest_openclaw_image_repo() == DEFAULT_OPENCLAW_IMAGE_REPO


def test_set_clawcu_home_persists_bootstrap_home_and_switches_store(temp_clawcu_home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    service, _, openclaw, store = make_service(temp_clawcu_home)
    new_home = tmp_path / "custom-home"

    saved = service.set_clawcu_home(str(new_home))

    assert saved == str(new_home.resolve())
    assert store.get_bootstrap_home() == str(new_home.resolve())
    assert store.paths.home == new_home.resolve()
    assert openclaw.store is store
    assert store.paths.instances_dir.exists()
    assert json.loads(bootstrap_config_path().read_text(encoding="utf-8")) == {
        "clawcu_home": str(new_home.resolve())
    }


def test_create_openclaw_saves_record(temp_clawcu_home, tmp_path) -> None:
    service, docker, openclaw, store = make_service(temp_clawcu_home)

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    assert record.status == "running"
    assert openclaw.versions == ["2026.4.1"]
    assert store.load_record("writer").image_tag == "ghcr.io/openclaw/openclaw:2026.4.1"
    assert docker.commands[0][0] == "run"


def test_create_service_uses_prepared_image_tag_override(temp_clawcu_home, tmp_path) -> None:
    service, docker, openclaw, store = make_service(temp_clawcu_home)

    def ensure_official(version: str) -> str:
        openclaw.versions.append(version)
        return f"ghcr.io/openclaw/openclaw:{version}"

    openclaw.ensure_image = ensure_official  # type: ignore[method-assign]

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    assert record.image_tag == "ghcr.io/openclaw/openclaw:2026.4.1"
    assert store.load_record("writer").image_tag == "ghcr.io/openclaw/openclaw:2026.4.1"
    assert openclaw.versions == ["2026.4.1"]
    assert docker.commands[0] == ("run", "clawcu-openclaw-writer")


def test_create_openclaw_makes_runtime_tree_writable(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    assert stat.S_IMODE(datadir.stat().st_mode) == 0o777
    assert stat.S_IMODE((datadir / "openclaw.json").stat().st_mode) == 0o666
    assert stat.S_IMODE((datadir / "workspace").stat().st_mode) == 0o777
    assert stat.S_IMODE((datadir / "workspace" / ".openclaw" / "workspace-state.json").stat().st_mode) == 0o666


def test_collect_providers_saves_directory_bundle_from_managed_instance(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    write_root_provider_source(datadir)
    write_provider_source(datadir)
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )

    result = service.collect_providers(instance="writer")

    assert result["saved"] == ["minimax (instance:writer)"]
    bundle = store.load_provider_bundle("minimax")
    assert "profiles" in bundle["auth_profiles"]
    assert list(bundle["models"]["providers"]) == ["minimax"]


def test_collect_providers_scans_all_instances(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    first = tmp_path / "writer-a"
    second = tmp_path / "writer-b"
    local_root = tmp_path / ".openclaw"
    write_root_provider_source(first, provider_name="minimax", profile_name="minimax:cn")
    write_root_provider_source(
        second,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    write_root_provider_source(
        local_root,
        provider_name="anthropic",
        profile_name="anthropic:default",
        api_key="sk-ant",
        api="anthropic-messages",
        endpoint="https://api.anthropic.com",
        models=[{"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"}],
    )
    write_provider_source(first, provider_name="minimax", profile_name="minimax:cn")
    write_provider_source(
        second,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    original_local = service._local_openclaw_home
    service._local_openclaw_home = lambda: local_root  # type: ignore[method-assign]
    for name, datadir in (("writer-a", first), ("writer-b", second)):
        store.save_record(
            InstanceRecord(
                service="openclaw",
                name=name,
                version="2026.4.1",
                upstream_ref="v2026.4.1",
                image_tag="clawcu/openclaw:2026.4.1",
                container_name=f"clawcu-openclaw-{name}",
                datadir=str(datadir),
                port=3000,
                cpu="1",
                memory="2g",
                auth_mode="token",
                status="running",
                created_at="2026-04-11T00:00:00+00:00",
                updated_at="2026-04-11T00:00:00+00:00",
                history=[],
            )
        )

    result = service.collect_providers(all_instances=True)

    service._local_openclaw_home = original_local  # type: ignore[method-assign]

    assert sorted(result["saved"]) == [
        "anthropic (path:" + str(local_root) + ")",
        "minimax (instance:writer-a)",
        "openai (instance:writer-b)",
    ]
    assert sorted(result["scanned"]) == sorted([str(first), str(local_root), str(second)])
    assert store.provider_exists("minimax")
    assert store.provider_exists("openai")
    assert store.provider_exists("anthropic")


def test_collect_providers_scans_all_instances_and_skips_sources_without_root_providers(
    temp_clawcu_home, tmp_path
) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    original_local = service._local_openclaw_home
    service._local_openclaw_home = lambda: tmp_path / ".missing-openclaw"  # type: ignore[method-assign]
    valid_root = tmp_path / "writer-valid"
    invalid_root = tmp_path / "writer-invalid"
    write_root_provider_source(valid_root, provider_name="minimax", profile_name="minimax:cn")
    invalid_root.mkdir(parents=True)
    (invalid_root / "openclaw.json").write_text("{}", encoding="utf-8")
    for name, datadir in (("writer-valid", valid_root), ("writer-invalid", invalid_root)):
        store.save_record(
            InstanceRecord(
                service="openclaw",
                name=name,
                version="2026.4.1",
                upstream_ref="v2026.4.1",
                image_tag="clawcu/openclaw:2026.4.1",
                container_name=f"clawcu-openclaw-{name}",
                datadir=str(datadir),
                port=3000,
                cpu="1",
                memory="2g",
                auth_mode="token",
                status="running",
                created_at="2026-04-11T00:00:00+00:00",
                updated_at="2026-04-11T00:00:00+00:00",
                history=[],
            )
        )

    result = service.collect_providers(all_instances=True)
    service._local_openclaw_home = original_local  # type: ignore[method-assign]

    assert result["saved"] == ["minimax (instance:writer-valid)"]
    assert sorted(result["scanned"]) == sorted([str(valid_root), str(invalid_root)])


def test_collect_providers_supports_external_path(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    external_root = tmp_path / ".openclaw"
    write_root_provider_source(external_root, provider_name="anthropic", profile_name="anthropic:default", api_key="sk-ant")
    write_provider_source(external_root, provider_name="anthropic", profile_name="anthropic:default", api_key="sk-ant")

    result = service.collect_providers(path=str(external_root))

    assert result["saved"] == [f"anthropic (path:{external_root})"]
    assert store.provider_exists("anthropic")


def test_collect_providers_splits_multiple_agents_and_numbers_name_collisions(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / "writer"
    write_root_provider_source(root, provider_name="minimax", api_key="sk-root")
    write_provider_source(root, agent_name="main", provider_name="minimax", api_key="sk-one")
    write_provider_source(root, agent_name="reviewer", provider_name="minimax", api_key="sk-two")

    result = service.collect_providers(path=str(root))

    assert sorted(result["saved"]) == [f"minimax (path:{root})"]
    assert result["merged"] == []
    assert store.provider_exists("minimax")
    assert not store.provider_exists("minimax-2")


def test_collect_providers_skips_identical_duplicates(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / "writer"
    write_root_provider_source(root, provider_name="minimax", api_key="sk-one")
    write_provider_source(root, agent_name="main", provider_name="minimax", api_key="sk-one")
    write_provider_source(root, agent_name="reviewer", provider_name="minimax", api_key="sk-one")

    result = service.collect_providers(path=str(root))

    assert result["saved"] == [f"minimax (path:{root})"]
    assert result["skipped"] == []
    assert store.provider_exists("minimax")
    assert not store.provider_exists("minimax-2")


def test_collect_providers_merges_models_when_provider_connection_matches(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / "writer"
    write_root_provider_source(
        root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    write_provider_source(
        root,
        agent_name="main",
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    write_provider_source(
        root,
        agent_name="reviewer",
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-4.1", "name": "GPT-4.1"}],
    )

    result = service.collect_providers(path=str(root))
    bundle = store.load_provider_bundle("openai")
    model_ids = service.list_provider_models("openai")

    assert result["saved"] == [f"openai (path:{root})"]
    assert result["merged"] == []
    assert not store.provider_exists("openai-2")
    assert model_ids == ["gpt-5"]
    assert list(bundle["auth_profiles"]["profiles"]) == ["openai:default"]


def test_collect_providers_prefers_root_openclaw_provider_over_agent_runtime(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / ".openclaw"
    write_root_provider_source(
        root,
        provider_name="zai-coding",
        api_key="sk-root-zai",
        api="openai-responses",
        endpoint="https://api.z.ai/api/coding/paas/v4",
        models=[{"id": "glm-5.1", "name": "GLM-5.1"}],
    )
    write_provider_source(
        root,
        provider_name="zai-coding",
        profile_name="zai-coding:default",
        api_key="sk-agent-zai",
        api="openai-responses",
        endpoint="https://api.z.ai/api/coding/paas/v4",
        models=[{"id": "glm-5", "name": "GLM-5"}],
    )

    result = service.collect_providers(path=str(root))
    bundle = store.load_provider_bundle("zai-coding")
    profiles = bundle["auth_profiles"]["profiles"]
    provider_payload = bundle["models"]["providers"]["zai-coding"]

    assert result["saved"] == [f"zai-coding (path:{root})"]
    assert result["merged"] == []
    assert not store.provider_exists("zai-coding-2")
    assert provider_payload["apiKey"] == "sk-root-zai"
    assert [model["id"] for model in provider_payload["models"]] == ["glm-5.1"]
    assert profiles == {
        "zai-coding:default": {
            "type": "api_key",
            "provider": "zai-coding",
            "key": "sk-root-zai",
        }
    }


def test_collect_providers_resolves_root_env_placeholders_for_managed_instance(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    write_root_provider_source(
        datadir,
        provider_name="openai",
        profile_name="openai:default",
        api_key="${OPENAI_API_KEY}",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    store.instance_env_path("writer").write_text("OPENAI_API_KEY=sk-managed\n", encoding="utf-8")

    service.collect_providers(instance="writer")
    bundle = store.load_provider_bundle("openai")

    assert bundle["models"]["providers"]["openai"]["apiKey"] == "sk-managed"
    assert bundle["auth_profiles"]["profiles"]["openai:default"]["key"] == "sk-managed"


def test_collect_providers_resolves_root_env_placeholders_for_external_path(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / ".openclaw"
    write_root_provider_source(
        root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="${OPENAI_API_KEY}",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    (root / ".env").write_text("OPENAI_API_KEY=sk-local\n", encoding="utf-8")

    service.collect_providers(path=str(root))
    bundle = store.load_provider_bundle("openai")

    assert bundle["models"]["providers"]["openai"]["apiKey"] == "sk-local"
    assert bundle["auth_profiles"]["profiles"]["openai:default"]["key"] == "sk-local"


def test_collect_providers_resolves_root_env_placeholders_for_managed_datadir_path(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    write_root_provider_source(
        datadir,
        provider_name="openai",
        profile_name="openai:default",
        api_key="${OPENAI_API_KEY}",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    store.instance_env_path("writer").write_text("OPENAI_API_KEY=sk-managed-path\n", encoding="utf-8")

    service.collect_providers(path=str(datadir))
    bundle = store.load_provider_bundle("openai")

    assert bundle["models"]["providers"]["openai"]["apiKey"] == "sk-managed-path"
    assert bundle["auth_profiles"]["profiles"]["openai:default"]["key"] == "sk-managed-path"


def test_list_show_and_remove_provider_use_directory_storage(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / "writer"
    write_root_provider_source(root, provider_name="anthropic", profile_name="anthropic:default", api_key="sk-ant")
    write_provider_source(root, provider_name="anthropic", profile_name="anthropic:default", api_key="sk-ant")
    service.collect_providers(path=str(root))

    providers = service.list_providers()
    shown = service.show_provider("anthropic")

    assert [provider["name"] for provider in providers] == ["anthropic"]
    assert shown["name"] == "anthropic"
    assert "auth_profiles" in shown
    assert "models" in shown

    service.remove_provider("anthropic")

    assert not store.provider_exists("anthropic")


def test_apply_provider_merges_bundle_into_agent_runtime_directory(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    source_root = tmp_path / "source"
    target_datadir = tmp_path / "writer"
    write_root_provider_source(
        source_root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    write_provider_source(
        source_root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    service.collect_providers(path=str(source_root))
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(target_datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    runtime_dir = target_datadir / "agents" / "chat" / "agent"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "anthropic": {
                        "api": "anthropic-messages",
                        "baseUrl": "https://api.anthropic.com",
                        "models": [{"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"}],
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "auth-profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "anthropic:default": {
                        "type": "api_key",
                        "provider": "anthropic",
                        "key": "sk-ant",
                    }
                },
                "lastGood": {"anthropic": "anthropic:default"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = service.apply_provider(
        "openai",
        "writer",
        "chat",
        primary="openai/gpt-5",
        fallbacks=["anthropic/claude-sonnet-4.5"],
    )

    assert result["agent"] == "chat"
    assert result["primary"] == "openai/gpt-5"
    assert result["fallbacks"] == "anthropic/claude-sonnet-4.5"
    assert result["env_key"] == "-"
    assert result["persist"] == "no"
    auth_payload = json.loads((runtime_dir / "auth-profiles.json").read_text(encoding="utf-8"))
    models_payload = json.loads((runtime_dir / "models.json").read_text(encoding="utf-8"))
    config_payload = json.loads((target_datadir / "openclaw.json").read_text(encoding="utf-8"))
    assert sorted(auth_payload["profiles"]) == ["anthropic:default", "openai:default"]
    assert sorted(models_payload["providers"]) == ["anthropic", "openai"]
    assert models_payload["providers"]["openai"]["models"] == [{"id": "gpt-5", "name": "GPT-5"}]
    assert sorted(config_payload["models"]["providers"]) == ["openai"]
    assert config_payload["models"]["providers"]["openai"]["models"] == [{"id": "gpt-5", "name": "GPT-5"}]
    assert "apiKey" not in config_payload["models"]["providers"]["openai"]
    assert config_payload["agents"]["list"] == [
        {
            "id": "chat",
            "model": {
                "primary": "openai/gpt-5",
                "fallbacks": ["anthropic/claude-sonnet-4.5"],
            },
        }
    ]
    assert not store.instance_env_path("writer").exists()


def test_apply_provider_persist_writes_env_backed_root_config(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    source_root = tmp_path / "source"
    target_datadir = tmp_path / "writer"
    write_root_provider_source(
        source_root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    write_provider_source(
        source_root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}],
    )
    service.collect_providers(path=str(source_root))
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(target_datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    runtime_dir = target_datadir / "agents" / "chat" / "agent"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    result = service.apply_provider(
        "openai",
        "writer",
        "chat",
        persist=True,
        primary="openai/gpt-5",
    )

    config_payload = json.loads((target_datadir / "openclaw.json").read_text(encoding="utf-8"))
    env_payload = store.instance_env_path("writer").read_text(encoding="utf-8")
    assert result["env_key"] == "CLAWCU_PROVIDER_OPENAI_API_KEY"
    assert result["persist"] == "yes"
    assert config_payload["models"]["providers"]["openai"]["apiKey"] == "${CLAWCU_PROVIDER_OPENAI_API_KEY}"
    assert "CLAWCU_PROVIDER_OPENAI_API_KEY=sk-openai" in env_payload


def test_provider_models_list_reads_collected_bundle(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    root = tmp_path / "writer"
    write_root_provider_source(
        root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}, {"id": "gpt-4.1", "name": "GPT-4.1"}],
    )
    write_provider_source(
        root,
        provider_name="openai",
        profile_name="openai:default",
        api_key="sk-openai",
        api="openai-responses",
        endpoint="https://api.openai.com/v1",
        models=[{"id": "gpt-5", "name": "GPT-5"}, {"id": "gpt-4.1", "name": "GPT-4.1"}],
    )
    service.collect_providers(path=str(root))

    models = service.list_provider_models("openai")

    assert models == ["gpt-5", "gpt-4.1"]


def test_collect_providers_ignores_agent_only_providers_not_declared_in_root_openclaw(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    root = tmp_path / ".openclaw"
    write_provider_source(root, provider_name="anthropic", profile_name="anthropic:default", api_key="sk-ant")

    result = service.collect_providers(path=str(root))

    assert result["saved"] == []
    assert result["merged"] == []
    assert result["skipped"] == []
    assert store.list_provider_names() == []


def test_collect_providers_requires_exactly_one_source_selector(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)

    with pytest.raises(ValueError, match="exactly one source"):
        service.collect_providers()

    with pytest.raises(ValueError, match="exactly one source"):
        service.collect_providers(all_instances=True, instance="writer")


def test_list_instance_summaries_include_instance_level_provider_and_models(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "api": "openai-responses",
                            "models": [
                                {"id": "gpt-5", "name": "GPT-5"},
                                {"id": "gpt-4.1", "name": "GPT-4.1"},
                            ],
                        },
                        "anthropic": {
                            "api": "anthropic-messages",
                            "models": [
                                {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"},
                            ],
                        },
                    }
                },
                "agents": {
                    "defaults": {
                        "model": {
                            "primary": "openai/gpt-5",
                            "fallbacks": [
                                "anthropic/claude-sonnet-4.5",
                            ],
                        }
                    },
                    "list": [
                        {
                            "id": "main",
                            "model": {
                                "primary": "openai/gpt-5",
                                "fallbacks": [
                                    "anthropic/claude-sonnet-4.5",
                                ],
                            },
                        },
                        {
                            "id": "chat",
                            "model": {
                                "primary": "anthropic/claude-sonnet-4.5",
                                "fallbacks": [
                                    "openai/gpt-4.1",
                                ],
                            },
                        },
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )

    summaries = service.list_instance_summaries()

    assert summaries[0]["providers"] == "anthropic, openai"
    assert summaries[0]["models"] == "anthropic/claude-sonnet-4.5, openai/gpt-4.1, openai/gpt-5"
    assert summaries[0]["home"] == str(datadir)
    assert "primary" not in summaries[0]
    assert "fallbacks" not in summaries[0]


def test_list_agent_summaries_include_primary_and_fallbacks_per_agent(temp_clawcu_home, tmp_path, monkeypatch) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "api": "openai-responses",
                            "models": [{"id": "gpt-5", "name": "GPT-5"}],
                        },
                        "anthropic": {
                            "api": "anthropic-messages",
                            "models": [{"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"}],
                        },
                    }
                },
                "agents": {
                    "list": [
                        {
                            "id": "main",
                            "model": {
                                "primary": "openai/gpt-5",
                                "fallbacks": ["anthropic/claude-sonnet-4.5"],
                            },
                        },
                        {
                            "id": "chat",
                            "model": {
                                "primary": "anthropic/claude-sonnet-4.5",
                                "fallbacks": ["openai/gpt-5"],
                            },
                        },
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    monkeypatch.setattr(service, "_persist_live_status", lambda record: record)

    summaries = service.list_agent_summaries()

    assert summaries == [
        {
            "source": "managed",
            "instance": "writer",
            "home": str(datadir),
            "service": "openclaw",
            "version": "2026.4.1",
            "port": 3000,
            "status": "running",
            "providers": "anthropic, openai",
            "models": "anthropic/claude-sonnet-4.5, openai/gpt-5",
            "agent": "main",
            "primary": "openai/gpt-5",
            "fallbacks": "anthropic/claude-sonnet-4.5",
        },
        {
            "source": "managed",
            "instance": "writer",
            "home": str(datadir),
            "service": "openclaw",
            "version": "2026.4.1",
            "port": 3000,
            "status": "running",
            "providers": "anthropic, openai",
            "models": "anthropic/claude-sonnet-4.5, openai/gpt-5",
            "agent": "chat",
            "primary": "anthropic/claude-sonnet-4.5",
            "fallbacks": "openai/gpt-5",
        },
    ]


def test_list_local_summaries_read_from_home_openclaw(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    openclaw_home = home / ".openclaw"
    openclaw_home.mkdir(parents=True)
    (openclaw_home / "openclaw.json").write_text(
        json.dumps(
            {
                "meta": {"lastTouchedVersion": "2026.4.9"},
                "models": {
                    "providers": {
                        "openai": {
                            "api": "openai-responses",
                            "models": [{"id": "gpt-5", "name": "GPT-5"}],
                        }
                    }
                },
                "agents": {
                    "list": [
                        {
                            "id": "main",
                            "model": {
                                "primary": "openai/gpt-5",
                                "fallbacks": ["openai/gpt-4.1"],
                            },
                        }
                    ]
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    service, _, _, _ = make_service(tmp_path / ".clawcu")
    service._local_openclaw_home = lambda: openclaw_home  # type: ignore[method-assign]

    instance_summaries = service.list_local_instance_summaries()
    agent_summaries = service.list_local_agent_summaries()

    assert instance_summaries == [
        {
            "source": "local",
            "name": "local-openclaw",
            "home": str(openclaw_home),
            "version": "2026.4.9",
            "port": 18789,
            "status": "local",
            "providers": "openai",
            "models": "openai/gpt-5",
            "service": "openclaw",
        }
    ]
    assert agent_summaries == [
            {
                "source": "local",
                "instance": "local-openclaw",
                "home": str(openclaw_home),
                "service": "openclaw",
                "version": "2026.4.9",
                "port": 18789,
            "status": "local",
            "providers": "openai",
            "models": "openai/gpt-5",
            "agent": "main",
            "primary": "openai/gpt-5",
            "fallbacks": "openai/gpt-4.1",
        }
    ]


def test_list_local_summaries_read_from_home_hermes(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n"
        "fallback_model:\n"
        "  provider: openrouter\n"
        "  model: openai/gpt-5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    service, _, _, _ = make_service(tmp_path / ".clawcu")
    service._local_hermes_home = lambda: hermes_home  # type: ignore[method-assign]

    def fake_runner(
        command,
        *,
        cwd=None,
        capture_output=True,
        check=True,
        stream_output=False,
        timeout_seconds=None,
    ):
        assert command == ["hermes", "version"]
        return type(
            "Completed",
            (),
            {
                "stdout": "Hermes Agent v0.8.0 (2026.4.8)\nProject: /tmp/hermes-agent\n",
                "stderr": "",
                "returncode": 0,
            },
        )()

    service.runner = fake_runner

    instance_summaries = service.list_local_instance_summaries()
    agent_summaries = service.list_local_agent_summaries()

    assert instance_summaries == [
        {
            "source": "local",
            "name": "local-hermes",
            "home": str(hermes_home),
            "version": "v0.8.0 (2026.4.8)",
            "port": 9119,
            "status": "local",
            "access_url": "http://127.0.0.1:9119/",
            "providers": "openrouter",
            "models": "anthropic/claude-sonnet-4.6, openrouter/openai/gpt-5",
            "service": "hermes",
        }
    ]
    assert agent_summaries == [
        {
            "source": "local",
            "instance": "local-hermes",
            "home": str(hermes_home),
            "service": "hermes",
            "version": "v0.8.0 (2026.4.8)",
            "port": 8642,
            "status": "local",
            "agent": "main",
            "primary": "anthropic/claude-sonnet-4.6",
            "fallbacks": "openrouter/openai/gpt-5",
            "providers": "openrouter",
            "models": "anthropic/claude-sonnet-4.6, openrouter/openai/gpt-5",
        }
    ]


def test_list_removed_instance_summaries_detect_orphaned_default_datadirs(temp_clawcu_home) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    active_datadir = temp_clawcu_home / "writer"
    active_datadir.mkdir()
    (active_datadir / "openclaw.json").write_text("{}", encoding="utf-8")
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(active_datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )

    removed_openclaw = temp_clawcu_home / "writer-old"
    removed_openclaw.mkdir()
    (removed_openclaw / "openclaw.json").write_text(
        json.dumps(
            {
                "meta": {"lastTouchedVersion": "2026.4.0"},
                "models": {
                    "providers": {
                        "openai": {
                            "api": "openai-responses",
                            "models": [{"id": "gpt-5", "name": "GPT-5"}],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    removed_hermes = temp_clawcu_home / "javis-old"
    removed_hermes.mkdir()
    (removed_hermes / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n"
        "fallback_model:\n"
        "  provider: openrouter\n"
        "  model: openai/gpt-5\n",
        encoding="utf-8",
    )

    hidden_orphan = temp_clawcu_home / ".hidden-orphan"
    hidden_orphan.mkdir()
    (hidden_orphan / "openclaw.json").write_text("{}", encoding="utf-8")
    (temp_clawcu_home / "notes").mkdir()

    summaries = service.list_removed_instance_summaries()
    summary_by_name = {summary["name"]: summary for summary in summaries}

    assert sorted(summary_by_name) == ["javis-old", "writer-old"]
    assert summary_by_name["writer-old"] == {
        "source": "removed",
        "name": "writer-old",
        "home": str(removed_openclaw),
        "version": "2026.4.0",
        "port": "-",
        "status": "removed",
        "access_url": "-",
        "providers": "openai",
        "models": "openai/gpt-5",
        "service": "openclaw",
        "snapshot": "-",
    }
    assert summary_by_name["javis-old"] == {
        "source": "removed",
        "name": "javis-old",
        "home": str(removed_hermes),
        "version": "-",
        "port": "-",
        "status": "removed",
        "access_url": "-",
        "providers": "openrouter",
        "models": "anthropic/claude-sonnet-4.6, openrouter/openai/gpt-5",
        "service": "hermes",
        "snapshot": "-",
    }


def test_list_agent_summaries_fall_back_to_managed_agent_directories(temp_clawcu_home, tmp_path, monkeypatch) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    (datadir / "agents" / "main" / "agent").mkdir(parents=True)
    (datadir / "agents" / "main" / "agent" / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "api": "openai-responses",
                        "models": [{"id": "gpt-5", "name": "GPT-5"}],
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (datadir / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": {
                            "primary": "openai/gpt-5",
                            "fallbacks": ["anthropic/claude-sonnet-4.5"],
                        }
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )
    monkeypatch.setattr(service, "_persist_live_status", lambda record: record)

    summaries = service.list_agent_summaries()

    assert summaries == [
        {
            "source": "managed",
            "instance": "writer",
            "home": str(datadir),
            "service": "openclaw",
            "version": "2026.4.1",
            "port": 3000,
            "status": "running",
            "providers": "openai",
            "models": "openai/gpt-5",
            "agent": "main",
            "primary": "openai/gpt-5",
            "fallbacks": "anthropic/claude-sonnet-4.5",
        }
    ]


def test_list_instance_summaries_fall_back_to_agent_runtime_provider_data(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    (datadir / "agents" / "main" / "agent").mkdir(parents=True)
    (datadir / "agents" / "chat" / "agent").mkdir(parents=True)
    (datadir / "agents" / "main" / "agent" / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "api": "openai-responses",
                        "models": [{"id": "gpt-5", "name": "GPT-5"}],
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (datadir / "agents" / "chat" / "agent" / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "anthropic": {
                        "api": "anthropic-messages",
                        "models": [{"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"}],
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (datadir / "openclaw.json").write_text("{}", encoding="utf-8")
    store.save_record(
        InstanceRecord(
            service="openclaw",
            name="writer",
            version="2026.4.1",
            upstream_ref="v2026.4.1",
            image_tag="clawcu/openclaw:2026.4.1",
            container_name="clawcu-openclaw-writer",
            datadir=str(datadir),
            port=3000,
            cpu="1",
            memory="2g",
            auth_mode="token",
            status="running",
            created_at="2026-04-11T00:00:00+00:00",
            updated_at="2026-04-11T00:00:00+00:00",
            history=[],
        )
    )

    summaries = service.list_instance_summaries()

    assert summaries[0]["providers"] == "anthropic, openai"
    assert summaries[0]["models"] == "anthropic/claude-sonnet-4.5, openai/gpt-5"


def test_create_openclaw_writes_gateway_config_to_datadir(temp_clawcu_home, tmp_path) -> None:
    import json

    service, docker, openclaw, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))
    assert config["gateway"]["bind"] == "lan"
    assert config["gateway"]["controlUi"]["allowedOrigins"] == ["*"]
    assert config["gateway"]["auth"]["mode"] == "token"


def test_dashboard_url_includes_token_fragment_when_available(temp_clawcu_home, tmp_path) -> None:
    import json

    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))
    config["gateway"]["auth"]["token"] = "abc123"
    (datadir / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")

    assert service.dashboard_url("writer") == "http://127.0.0.1:3000/#token=abc123"


def test_token_returns_dashboard_token_when_available(temp_clawcu_home, tmp_path) -> None:
    import json

    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))
    config["gateway"]["auth"]["token"] = "abc123"
    (datadir / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")

    assert service.token("writer") == "abc123"


def test_token_returns_bootstrapped_dashboard_token(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    token = service.token("writer")

    assert token
    assert len(token) == 48


def test_create_openclaw_times_out_when_health_never_becomes_ready(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.STARTUP_TIMEOUT_SECONDS = 0.01
    service.STARTUP_POLL_INTERVAL_SECONDS = 0
    service.STARTUP_PROGRESS_INTERVAL_SECONDS = 0
    docker.startup_sequences["clawcu-openclaw-writer"] = ["starting"]

    with pytest.raises(RuntimeError, match="did not become ready within"):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=3000,
            cpu="1",
            memory="2g",
        )

    record = store.load_record("writer")
    assert record.last_error is not None
    assert "did not become ready within" in record.last_error
    assert record.history[-1]["action"] == "startup_failed"


def test_create_hermes_times_out_when_dashboard_never_becomes_ready(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.STARTUP_TIMEOUT_SECONDS = 0.01
    service.STARTUP_POLL_INTERVAL_SECONDS = 0
    service.STARTUP_PROGRESS_INTERVAL_SECONDS = 0
    docker.startup_sequences["clawcu-hermes-javis"] = ["starting"]
    hermes_adapter = service.adapter_for_service("hermes")
    hermes_adapter._dashboard_ready = lambda _record: False  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="did not become ready within"):
        service.create_hermes(
            name="javis",
            version="2026.4.13",
            datadir=str(tmp_path / "javis"),
            port=8652,
            cpu="1",
            memory="2g",
        )

    record = store.load_record("javis")
    assert record.last_error is not None
    assert "did not become ready within" in record.last_error
    assert record.history[-1]["action"] == "startup_failed"


def test_remove_instance_preserves_record_when_container_removal_fails(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    docker.fail_next_remove = True

    with pytest.raises(RuntimeError, match="Failed to remove Docker container"):
        service.remove_instance("writer", delete_data=True)

    record = store.load_record("writer")
    assert record.last_error is not None
    assert "docker rm timed out" in record.last_error
    assert record.history[-1]["action"] == "remove_failed"
    assert datadir.exists()


def test_remove_removed_instance_deletes_orphaned_datadir_and_stale_container(
    temp_clawcu_home,
) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = temp_clawcu_home / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.remove_instance("writer", delete_data=False)
    docker.status_map["clawcu-openclaw-writer"] = "exited"

    service.remove_removed_instance("writer")

    assert not datadir.exists()
    assert store.instance_path("writer").exists() is False
    assert docker.commands[-1] == ("rm", "clawcu-openclaw-writer")


def test_remove_removed_instance_preserves_orphaned_datadir_when_container_removal_fails(
    temp_clawcu_home,
) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    datadir = temp_clawcu_home / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.remove_instance("writer", delete_data=False)
    docker.status_map["clawcu-openclaw-writer"] = "exited"
    docker.fail_next_remove = True

    with pytest.raises(RuntimeError, match="Failed to remove Docker container for removed instance"):
        service.remove_removed_instance("writer")

    assert datadir.exists()


def test_set_instance_env_writes_instance_env_file(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    result = service.set_instance_env(
        "writer",
        ["OPENAI_API_KEY=sk-test", "OPENAI_BASE_URL=https://api.example.com/v1"],
    )
    env_path = store.instance_env_path("writer")

    assert result["path"] == str(env_path)
    assert result["updated_keys"] == ["OPENAI_API_KEY", "OPENAI_BASE_URL"]
    assert env_path.read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=sk-test\n"
        "OPENAI_BASE_URL=https://api.example.com/v1\n"
    )


def test_set_instance_env_overwrites_existing_keys(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=old-value\nOTHER=value\n", encoding="utf-8")

    service.set_instance_env("writer", ["OPENAI_API_KEY=new-value"])

    assert env_path.read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=new-value\n"
        "OTHER=value\n"
    )


def test_get_instance_env_reads_instance_env_file(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=https://api.example.com/v1\n", encoding="utf-8")

    result = service.get_instance_env("writer")

    assert result["path"] == str(env_path)
    assert result["values"] == {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://api.example.com/v1",
    }


def test_get_instance_env_returns_empty_values_when_env_file_missing(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    result = service.get_instance_env("writer")

    assert result["values"] == {}


def test_unset_instance_env_removes_existing_keys(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text(
        "OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=https://api.example.com/v1\nOTHER=value\n",
        encoding="utf-8",
    )

    result = service.unset_instance_env("writer", ["OPENAI_API_KEY", "MISSING_KEY"])

    assert result["removed_keys"] == ["OPENAI_API_KEY"]
    assert env_path.read_text(encoding="utf-8") == (
        "OPENAI_BASE_URL=https://api.example.com/v1\n"
        "OTHER=value\n"
    )


def test_unset_instance_env_keeps_file_when_no_keys_match(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")

    result = service.unset_instance_env("writer", ["MISSING_KEY"])

    assert result["removed_keys"] == []
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-test\n"


def test_approve_pairing_uses_latest_pending_request(temp_clawcu_home, tmp_path) -> None:
    import json

    service, docker, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    devices_dir = datadir / "devices"
    devices_dir.mkdir(exist_ok=True)
    (devices_dir / "pending.json").write_text(
        json.dumps(
            {
                "old": {"requestId": "old", "ts": 1},
                "new": {"requestId": "new", "ts": 2},
            }
        ),
        encoding="utf-8",
    )

    approved = service.approve_pairing("writer")

    assert approved == "new"
    assert docker.exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "devices", "approve", "new"],
        {"env": {}},
    )


def test_approve_pairing_accepts_explicit_request_id(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    approved = service.approve_pairing("writer", request_id="manual-id")

    assert approved == "manual-id"
    assert docker.exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "devices", "approve", "manual-id"],
        {"env": {}},
    )


def test_approve_pairing_passes_instance_env_to_docker_exec(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text(
        "CLAWCU_PROVIDER_KIMI_CODING_API_KEY=sk-kimi\n",
        encoding="utf-8",
    )

    approved = service.approve_pairing("writer", request_id="manual-id")

    assert approved == "manual-id"
    assert docker.exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "devices", "approve", "manual-id"],
        {"env": {"CLAWCU_PROVIDER_KIMI_CODING_API_KEY": "sk-kimi"}},
    )


def test_approve_pairing_raises_when_no_pending_requests(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(ValueError, match="has no pending pairing requests"):
        service.approve_pairing("writer")


def test_configure_instance_runs_official_configure_command(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.configure_instance("writer", extra_args=["--section", "models"])

    assert docker.interactive_exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "configure", "--section", "models"],
        {"env": {}},
    )


def test_exec_instance_runs_arbitrary_command_in_container(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.exec_instance("writer", ["pwd"])

    assert docker.interactive_exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["pwd"],
        {"env": {}},
    )


def test_configure_instance_passes_instance_env_to_docker_exec(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text(
        "OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=https://api.example.com/v1\n",
        encoding="utf-8",
    )

    service.configure_instance("writer")

    assert docker.interactive_exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "configure"],
        {
            "env": {
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_BASE_URL": "https://api.example.com/v1",
            }
        },
    )


def test_exec_instance_passes_instance_env_to_docker_exec(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text(
        "CLAWCU_PROVIDER_KIMI_CODING_API_KEY=sk-kimi\n",
        encoding="utf-8",
    )

    service.exec_instance("writer", ["node", "openclaw.mjs", "tui"])

    assert docker.interactive_exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "tui"],
        {"env": {"CLAWCU_PROVIDER_KIMI_CODING_API_KEY": "sk-kimi"}},
    )


def test_exec_instance_requires_a_command(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)

    with pytest.raises(ValueError, match="Please provide a command"):
        service.exec_instance("writer", [])


def test_tui_instance_auto_approves_latest_pending_request(temp_clawcu_home, tmp_path) -> None:
    import json

    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text(
        "CLAWCU_PROVIDER_KIMI_CODING_API_KEY=sk-kimi\n",
        encoding="utf-8",
    )
    devices_dir = datadir / "devices"
    devices_dir.mkdir(exist_ok=True)
    (devices_dir / "pending.json").write_text(
        json.dumps({"new": {"requestId": "new", "ts": 2}}),
        encoding="utf-8",
    )

    service.tui_instance("writer")

    assert docker.exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["node", "openclaw.mjs", "devices", "approve", "new"],
        {"env": {"CLAWCU_PROVIDER_KIMI_CODING_API_KEY": "sk-kimi"}},
    )
    assert docker.interactive_exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["openclaw", "tui"],
        {"env": {"CLAWCU_PROVIDER_KIMI_CODING_API_KEY": "sk-kimi"}},
    )


def test_tui_instance_launches_requested_agent_without_pending_request(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.tui_instance("writer", agent="chat")

    assert docker.interactive_exec_commands[-1] == (
        "clawcu-openclaw-writer",
        ["openclaw", "tui", "--agent", "chat"],
        {"env": {}},
    )


def test_create_openclaw_recovers_from_empty_gateway_config_file(temp_clawcu_home, tmp_path) -> None:
    import json

    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "openclaw.json").write_text("", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))
    assert config["gateway"]["bind"] == "lan"
    assert config["gateway"]["controlUi"]["allowedOrigins"] == ["*"]
    assert config["gateway"]["auth"]["mode"] == "token"


def test_create_openclaw_defaults_datadir_and_port(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port == 18799

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        cpu="1",
        memory="2g",
    )

    assert record.datadir.endswith("/.clawcu/writer")
    assert record.port == 18799
    assert record.auth_mode == "token"


def test_create_openclaw_rejects_duplicate_name(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(ValueError, match="Instance 'writer' already exists."):
        service.create_openclaw(
            name="writer",
            version="2026.4.2",
            datadir=str(tmp_path / "writer-2"),
            port=18799,
            cpu="1",
            memory="2g",
        )

    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 1


def test_create_openclaw_rejects_existing_docker_container_without_record(temp_clawcu_home) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    docker.status_map["clawcu-openclaw-writer"] = "created"

    with pytest.raises(
        ValueError,
        match="Instance 'writer' already exists. Docker container 'clawcu-openclaw-writer' is already present.",
        ):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            port=18789,
            cpu="1",
            memory="2g",
        )

    assert ("run", "clawcu-openclaw-writer") not in docker.commands


def test_create_openclaw_searches_next_port_by_ten(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port == 18819

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        cpu="1",
        memory="2g",
    )

    assert record.port == 18819


def test_create_openclaw_retries_next_port_when_docker_bind_races(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port in {18799, 18809}
    docker.run_errors.append(
        CommandError(
            ["docker", "run"],
            125,
            "",
            "Bind for 0.0.0.0:18799 failed: port is already allocated",
        )
    )

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        cpu="1",
        memory="2g",
    )

    assert record.port == 18809
    assert ("rm", "clawcu-openclaw-writer") in docker.commands
    stored = store.load_record("writer")
    assert stored.port == 18809
    assert stored.last_error is None
    assert [event["action"] for event in stored.history] == [
        "create_requested",
        "create_failed",
        "created",
    ]


def test_create_openclaw_persists_failed_record(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError, match="Failed to create instance 'writer'"):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    stored = store.load_record("writer")
    assert stored.status == "create_failed"
    assert stored.last_error == "boom"
    assert stored.port == 18789
    assert stored.history[-1]["action"] == "create_failed"
    assert "boom" in stored.history[-1]["error"]


def test_list_instances_preserves_failed_create_status(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    records = service.list_instances()
    assert len(records) == 1
    assert records[0].name == "writer"
    assert records[0].status == "create_failed"
    assert records[0].last_error == "boom"


def test_retry_instance_recreates_failed_instance(temp_clawcu_home, tmp_path) -> None:
    service, docker, openclaw, store = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    retried = service.retry_instance("writer")

    assert retried.status == "running"
    assert retried.port == 18789
    assert openclaw.versions[-1] == "2026.4.1"
    stored = store.load_record("writer")
    assert stored.status == "running"
    assert stored.last_error is None
    assert [event["action"] for event in stored.history[-3:]] == [
        "create_failed",
        "retry_requested",
        "created",
    ]
    assert ("rm", "clawcu-openclaw-writer") in docker.commands


def test_start_instance_persists_start_failed_status(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.stop_instance("writer")
    docker.fail_next_start = True

    with pytest.raises(RuntimeError, match="Failed to start instance 'writer'"):
        service.start_instance("writer")

    stored = store.load_record("writer")
    assert stored.status == "start_failed"
    assert stored.last_error == "port is already allocated"
    assert stored.history[-1]["action"] == "start_failed"
    listed = service.list_instances()
    assert listed[0].status == "start_failed"


def test_start_instance_clears_start_failed_after_successful_retry(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.stop_instance("writer")
    docker.fail_next_start = True

    with pytest.raises(RuntimeError):
        service.start_instance("writer")

    started = service.start_instance("writer")

    assert started.status == "running"
    assert store.load_record("writer").last_error is None


def test_start_instance_reports_progress(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    messages: list[str] = []
    service.set_reporter(messages.append)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.stop_instance("writer")
    messages.clear()

    started = service.start_instance("writer")

    assert started.status == "running"
    assert messages[0] == "Step 1/3: Loading the saved OpenClaw instance record for 'writer'."
    assert messages[1] == "Step 2/3: Starting Docker container clawcu-openclaw-writer. This usually takes a few seconds."
    assert messages[2] == "Step 3/3: Refreshing live status after Docker start."
    assert messages[3].startswith("OpenClaw reported status 'running'. Access URL: http://127.0.0.1:18789/")


def test_start_instance_recreates_when_container_env_is_stale(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.stop_instance("writer")
    store.instance_env_path("writer").write_text("OPENAI_API_KEY=sk-managed\n", encoding="utf-8")

    original_inspect = docker.inspect_container

    def inspect_with_stale_env(container_name: str):
        payload = original_inspect(container_name)
        if payload is None:
            return None
        payload["Config"] = {"Env": []}
        return payload

    docker.inspect_container = inspect_with_stale_env  # type: ignore[method-assign]

    started = service.start_instance("writer")

    assert started.status == "running"
    assert ("start", "clawcu-openclaw-writer") not in docker.commands
    assert ("rm", "clawcu-openclaw-writer") in docker.commands
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 2
    assert service.openclaw.versions == ["2026.4.1"]


def test_restart_instance_default_uses_docker_restart(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    service.restart_instance("writer")

    # Plain restart: docker restart is issued and no extra run/rm happens.
    assert ("restart", "clawcu-openclaw-writer") in docker.commands
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 1
    assert ("rm", "clawcu-openclaw-writer") not in docker.commands


def test_restart_instance_recreate_if_config_changed_promotes_on_env_drift(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    # Simulate `clawcu setenv` without --apply: env file has a new key
    # that the live container was never started with.
    store.instance_env_path("writer").write_text(
        "OPENAI_API_KEY=sk-managed\n", encoding="utf-8"
    )

    original_inspect = docker.inspect_container

    def inspect_with_stale_env(container_name: str):
        payload = original_inspect(container_name)
        if payload is None:
            return None
        payload["Config"] = {"Env": []}
        return payload

    docker.inspect_container = inspect_with_stale_env  # type: ignore[method-assign]

    restarted = service.restart_instance("writer", recreate_if_config_changed=True)

    assert restarted.status == "running"
    # Promotion path: we should have gone through rm + re-run, not docker restart.
    assert ("restart", "clawcu-openclaw-writer") not in docker.commands
    assert ("rm", "clawcu-openclaw-writer") in docker.commands
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 2


def test_restart_instance_recreate_if_config_changed_no_drift_stays_fast(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    # No env file, no drift — the flag should be a no-op and we should
    # take the cheap docker-restart path.
    service.restart_instance("writer", recreate_if_config_changed=True)

    assert ("restart", "clawcu-openclaw-writer") in docker.commands
    assert ("rm", "clawcu-openclaw-writer") not in docker.commands
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 1


def test_restart_instance_recreate_if_config_changed_handles_missing_container(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    docker.remove_container("clawcu-openclaw-writer")

    restarted = service.restart_instance("writer", recreate_if_config_changed=True)

    assert restarted.status == "running"
    # No plain `docker restart` against a missing container; we must
    # have re-run from scratch.
    assert ("restart", "clawcu-openclaw-writer") not in docker.commands
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 2


def test_stop_instance_passes_timeout_to_docker(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    service.stop_instance("writer", timeout=42)

    assert ("stop", "clawcu-openclaw-writer", 42) in docker.commands


def test_start_instance_recreates_when_container_is_missing(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    docker.remove_container("clawcu-openclaw-writer")

    started = service.start_instance("writer")

    assert started.status == "running"
    assert ("start", "clawcu-openclaw-writer") not in docker.commands
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 2
    assert store.load_record("writer").status == "running"
    assert service.openclaw.versions == ["2026.4.1"]


def test_retry_instance_rejects_non_failed_instance(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(
        ValueError,
        match="Instance 'writer' is in status 'running'. Only create_failed instances can be retried.",
    ):
        service.retry_instance("writer")


def test_create_openclaw_rejects_duplicate_name_after_failed_create(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    with pytest.raises(ValueError, match="Instance 'writer' already exists."):
        service.create_openclaw(
            name="writer",
            version="2026.4.2",
            datadir=str(tmp_path / "writer-2"),
            port=18799,
            cpu="1",
            memory="2g",
        )


def test_create_openclaw_records_clawcu_version(temp_clawcu_home, tmp_path) -> None:
    from clawcu import __version__

    service, docker, openclaw, store = make_service(temp_clawcu_home)

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    stored = store.load_record("writer")
    create_event = stored.history[0]
    assert create_event["action"] == "create_requested"
    assert create_event["clawcu_version"] == __version__
    assert create_event["auth_mode"] == "token"


def test_recreate_instance_rebuilds_container(temp_clawcu_home, tmp_path) -> None:
    service, docker, openclaw, store = make_service(temp_clawcu_home)

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    recreated = service.recreate_instance("writer")

    assert recreated.status == "running"
    assert recreated.port == 3000
    assert recreated.version == "2026.4.1"
    assert ("rm", "clawcu-openclaw-writer") in docker.commands
    # run should be called twice: original create + recreate
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 2

    stored = store.load_record("writer")
    actions = [e["action"] for e in stored.history]
    assert "recreate_requested" in actions
    recreate_event = next(e for e in stored.history if e["action"] == "recreate_requested")
    assert "clawcu_version" in recreate_event


def test_recreate_instance_with_timeout_stops_gracefully_before_rm(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.recreate_instance("writer", timeout=42)

    cname = "clawcu-openclaw-writer"
    assert ("stop", cname, 42) in docker.commands
    rm_index = docker.commands.index(("rm", cname))
    stop_index = docker.commands.index(("stop", cname, 42))
    assert stop_index < rm_index, "graceful stop must precede the force-remove"


def test_recreate_instance_fresh_wipes_datadir_contents(
    temp_clawcu_home, tmp_path
) -> None:
    service, _docker, _openclaw, _store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    (datadir / "stale_file.txt").write_text("leftover")
    nested = datadir / "subdir"
    nested.mkdir(exist_ok=True)
    (nested / "deep.bin").write_bytes(b"\x00\x01\x02")

    service.recreate_instance("writer", fresh=True)

    assert datadir.exists()
    assert not (datadir / "stale_file.txt").exists()
    assert not (datadir / "subdir").exists()


def test_recreate_instance_fresh_refuses_unsafe_datadir(
    temp_clawcu_home, tmp_path
) -> None:
    service, _docker, _openclaw, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    record = store.load_record("writer")
    from pathlib import Path as _Path

    home = _Path.home()
    updated = record.__class__(**{**record.__dict__, "datadir": str(home)})
    store.save_record(updated)

    with pytest.raises(ValueError, match="Refusing to wipe unsafe datadir"):
        service.recreate_instance("writer", fresh=True)


def test_recreate_hermes_preserves_dashboard_port(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port == 9129  # type: ignore[method-assign]
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]

    created = service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(tmp_path / "scribe"),
        port=8652,
        cpu="1",
        memory="2g",
    )

    recreated = service.recreate_instance("scribe")

    assert created.dashboard_port == 9129
    assert recreated.port == 8652
    assert recreated.dashboard_port == 9129
    assert ("rm", "clawcu-hermes-scribe") in docker.commands
    assert store.load_record("scribe").dashboard_port == 9129


def test_recreate_removed_openclaw_restores_orphaned_datadir(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = temp_clawcu_home / "writer"
    created = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.remove_instance("writer", delete_data=False)

    removed = service.list_removed_instance_summaries()
    assert removed == [
        {
            "source": "removed",
            "name": "writer",
            "home": str(datadir),
            "version": "2026.4.1",
            "port": 3000,
            "status": "removed",
            "access_url": "-",
            "providers": "-",
            "models": "-",
            "service": "openclaw",
            "snapshot": "-",
        }
    ]

    recreated = service.recreate_instance("writer")

    assert recreated.status == "running"
    assert recreated.service == "openclaw"
    assert recreated.version == created.version
    assert recreated.datadir == str(datadir)
    assert store.load_record("writer").version == created.version
    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 2


def test_recreate_removed_hermes_restores_orphaned_datadir_from_saved_metadata(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    datadir = temp_clawcu_home / "scribe"

    created = service.create_hermes(
        name="scribe",
        version="2026.4.8",
        datadir=str(datadir),
        port=8652,
        cpu="1",
        memory="2g",
    )

    service.remove_instance("scribe", delete_data=False)

    removed = service.list_removed_instance_summaries()
    assert removed == [
        {
            "source": "removed",
            "name": "scribe",
            "home": str(datadir),
            "version": created.version,
            "port": 8652,
            "status": "removed",
            "access_url": "-",
            "providers": "openrouter",
            "models": "anthropic/claude-sonnet-4.6",
            "service": "hermes",
            "snapshot": "-",
        }
    ]

    recreated = service.recreate_instance("scribe")

    assert recreated.status == "running"
    assert recreated.service == "hermes"
    assert recreated.version == created.version
    assert recreated.datadir == str(datadir)
    assert store.load_record("scribe").version == created.version
    assert docker.commands.count(("run", "clawcu-hermes-scribe")) == 2


def test_recreate_removed_hermes_requires_explicit_version_without_metadata(
    temp_clawcu_home,
) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    root = temp_clawcu_home / "legacy-hermes"
    root.mkdir()
    (root / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not record its version"):
        service.recreate_instance("legacy-hermes")


def test_create_openclaw_waits_for_healthcheck_until_running(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    service.STARTUP_POLL_INTERVAL_SECONDS = 0.0
    messages: list[str] = []
    service.set_reporter(messages.append)
    docker.startup_sequences["clawcu-openclaw-writer"] = ["starting", "starting", "running"]

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    assert record.status == "running"
    assert any("Waiting for OpenClaw to become ready" in message for message in messages)
    assert any("docker ps --filter name=clawcu-openclaw-writer" in message for message in messages)
    assert any("clawcu inspect writer" in message for message in messages)
    assert any("still starting" in message for message in messages)
    assert any("clawcu logs writer" in message for message in messages)
    assert sum("docker ps --filter name=clawcu-openclaw-writer" in message for message in messages) == 1


def test_create_openclaw_bootstraps_workspace_and_local_gateway_mode(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))
    workspace_state = json.loads(
        (datadir / "workspace" / ".openclaw" / "workspace-state.json").read_text(
            encoding="utf-8"
        )
    )

    assert record.status == "running"
    assert config["gateway"]["mode"] == "local"
    assert config["gateway"]["bind"] == "lan"
    assert config["gateway"]["port"] == 18789
    assert config["gateway"]["controlUi"]["allowedOrigins"] == ["*"]
    assert config["gateway"]["controlUi"]["dangerouslyAllowHostHeaderOriginFallback"] is True
    assert config["agents"]["defaults"]["workspace"] == "/home/node/.openclaw/workspace"
    assert workspace_state["version"] == 1
    assert workspace_state["onboardingCompletedAt"]
    assert (datadir / "workspace" / "memory").is_dir()


def test_create_openclaw_prefers_host_health_endpoint_over_docker_starting(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.STARTUP_POLL_INTERVAL_SECONDS = 0.0
    messages: list[str] = []
    service.set_reporter(messages.append)
    docker.startup_sequences["clawcu-openclaw-writer"] = ["starting", "starting", "starting"]
    service._host_healthcheck_ready = lambda record: True

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    assert record.status == "running"
    assert store.load_record("writer").status == "running"
    assert any("health endpoint is responding" in message for message in messages)


def test_inspect_instance_includes_snapshot_summary(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    record.history.extend(
        [
            {
                "action": "upgrade",
                "timestamp": "2026-04-15T00:00:00+00:00",
                "from_version": "2026.4.1",
                "to_version": "2026.4.10",
                "snapshot_dir": "/tmp/upgrade-snapshot",
            },
            {
                "action": "rollback",
                "timestamp": "2026-04-15T01:00:00+00:00",
                "from_version": "2026.4.10",
                "to_version": "2026.4.1",
                "snapshot_dir": "/tmp/rollback-snapshot",
                "restored_snapshot": "/tmp/upgrade-snapshot",
            },
        ]
    )
    store.save_record(record)

    payload = service.inspect_instance("writer")

    assert payload["snapshots"] == {
        "latest_upgrade_snapshot": "/tmp/upgrade-snapshot",
        "latest_rollback_snapshot": "/tmp/rollback-snapshot",
        "latest_restored_snapshot": "/tmp/upgrade-snapshot",
    }
    assert payload["container"]["Name"] == "clawcu-openclaw-writer"


def test_list_instance_summaries_includes_latest_snapshot_label(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    record.history.extend(
        [
            {
                "action": "upgrade",
                "timestamp": "2026-04-15T00:00:00+00:00",
                "from_version": "2026.4.1",
                "to_version": "2026.4.10",
                "snapshot_dir": "/tmp/upgrade-snapshot",
            },
            {
                "action": "rollback",
                "timestamp": "2026-04-15T01:00:00+00:00",
                "from_version": "2026.4.10",
                "to_version": "2026.4.1",
                "snapshot_dir": "/tmp/rollback-snapshot",
                "restored_snapshot": "/tmp/upgrade-snapshot",
            },
        ]
    )
    store.save_record(record)

    summaries = service.list_instance_summaries()

    assert summaries[0]["snapshot"] == "rollback 2026.4.10 -> 2026.4.1"


def test_host_healthcheck_treats_connection_reset_as_not_ready(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    adapter = service.adapters["openclaw"]
    record = InstanceRecord(
        service="openclaw",
        name="writer",
        version="2026.4.1",
        upstream_ref="v2026.4.1",
        image_tag="clawcu/openclaw:2026.4.1",
        container_name="clawcu-openclaw-writer",
        datadir="/tmp/writer",
        port=3000,
        cpu="1",
        memory="2g",
        auth_mode="token",
        status="starting",
        created_at="2026-04-11T00:00:00+00:00",
        updated_at="2026-04-11T00:00:00+00:00",
        history=[],
    )

    original_urlopen = urllib.request.urlopen

    def fake_urlopen(*args, **kwargs):
        raise ConnectionResetError(54, "Connection reset by peer")

    urllib.request.urlopen = fake_urlopen
    try:
        assert adapter._host_healthcheck_ready(record) is False
    finally:
        urllib.request.urlopen = original_urlopen


def test_create_openclaw_raises_when_startup_becomes_unhealthy(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.STARTUP_POLL_INTERVAL_SECONDS = 0.0
    docker.startup_sequences["clawcu-openclaw-writer"] = ["starting", "unhealthy"]

    with pytest.raises(RuntimeError, match="did not become ready"):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=3000,
            cpu="1",
            memory="2g",
        )

    stored = store.load_record("writer")
    assert stored.status == "unhealthy"
    assert stored.last_error is not None
    assert any(event["action"] == "startup_failed" for event in stored.history)


def test_recreate_instance_normalizes_legacy_auth_mode_to_token(temp_clawcu_home, tmp_path) -> None:
    import json

    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    payload = store.load_record("writer").to_dict()
    payload["auth_mode"] = "none"
    store.instance_path("writer").write_text(json.dumps(payload), encoding="utf-8")

    recreated = service.recreate_instance("writer")

    stored = store.load_record("writer")
    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))
    assert recreated.auth_mode == "token"
    assert stored.auth_mode == "token"
    assert config["gateway"]["auth"]["mode"] == "token"


def test_loading_legacy_record_defaults_auth_mode_to_token(temp_clawcu_home, tmp_path) -> None:
    import json

    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    payload = record.to_dict()
    payload.pop("auth_mode")
    store.instance_path("writer").write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load_record("writer")
    assert loaded.auth_mode == "token"


def test_upgrade_rolls_back_when_new_container_fails(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    messages: list[str] = []
    service.set_reporter(messages.append)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("stable", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text("OPENAI_API_KEY=stable\n", encoding="utf-8")

    original_run_container = docker.run_container
    failed_once = False

    def fail_upgrade_run(record, spec) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            store.instance_env_path("writer").write_text("OPENAI_API_KEY=changed\n", encoding="utf-8")
            raise RuntimeError("boom")
        original_run_container(record, spec)

    docker.run_container = fail_upgrade_run  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        service.upgrade_instance("writer", version="2026.4.2")

    record = store.load_record("writer")
    assert record.version == "2026.4.1"
    assert record.status == "running"
    assert record.history[-1]["action"] == "upgrade_failed"
    assert store.instance_env_path("writer").read_text(encoding="utf-8") == "OPENAI_API_KEY=stable\n"
    assert any("Trying to restore 2026.4.1 from the snapshot." in message for message in messages)


def test_upgrade_plan_returns_preview_without_side_effects(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text(
        "OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=https://api.example.com/v1\n",
        encoding="utf-8",
    )

    # Record docker commands before the call so we can prove no new
    # container lifecycle commands were issued by the plan path.
    commands_before = list(docker.commands)

    plan = service.upgrade_plan("writer", version="2026.4.2")

    assert plan["instance"] == "writer"
    assert plan["current_version"] == "2026.4.1"
    assert plan["target_version"] == "2026.4.2"
    assert plan["env_exists"] is True
    assert set(plan["env_keys"]) == {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
    assert plan["env_carryover"] == "preserved"
    assert plan["projected_image"] == "ghcr.io/openclaw/openclaw:2026.4.2"
    assert plan["snapshot_label"] == "upgrade-to-2026.4.2"
    assert str(plan["snapshot_root"]).endswith("snapshots/writer")
    # No new docker calls — the plan is pure read.
    assert docker.commands == commands_before


def test_upgrade_plan_rejects_same_version(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(ValueError, match="already on version"):
        service.upgrade_plan("writer", version="2026.4.1")


def test_list_upgradable_versions_merges_history_and_local_images(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, openclaw, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    # Pretend we already upgraded once so the history has a prior version.
    service.upgrade_instance("writer", version="2026.4.2")
    # Stub the docker layer to report what's locally available.
    docker.list_local_images = lambda repo: ["2026.4.1", "2026.4.2", "2026.4.3"]  # type: ignore[method-assign]

    payload = service.list_upgradable_versions("writer")

    assert payload["instance"] == "writer"
    assert payload["image_repo"] == openclaw.image_repo
    assert payload["current_version"] == "2026.4.2"
    # History covers both versions (from + to).
    assert "2026.4.1" in payload["history"]
    assert "2026.4.2" in payload["history"]
    # Local images passed through unchanged.
    assert payload["local_images"] == ["2026.4.1", "2026.4.2", "2026.4.3"]


def test_list_upgradable_versions_includes_remote_tags_by_default(
    temp_clawcu_home, tmp_path
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    # Simulate a successful registry query.
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.1", "2026.4.2", "2026.4.3"],
        registry="ghcr.io",
    )

    payload = service.list_upgradable_versions("writer")

    assert payload["remote_requested"] is True
    assert payload["remote_versions"] == ["2026.4.1", "2026.4.2", "2026.4.3"]
    assert payload["remote_error"] is None
    assert payload["remote_registry"] == "ghcr.io"
    # The manager must have been asked — default include_remote=True.
    assert openclaw.list_remote_versions_calls  # at least one call


def test_list_upgradable_versions_surfaces_remote_failure(
    temp_clawcu_home, tmp_path
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    openclaw.remote_result = _FakeRemoteTagResult(
        error="network error: timeout",
        registry="ghcr.io",
    )

    payload = service.list_upgradable_versions("writer")

    # Remote failed -> remote_versions is None but the overall call
    # must still succeed (best-effort contract).
    assert payload["remote_versions"] is None
    assert payload["remote_error"] == "network error: timeout"
    assert payload["remote_registry"] == "ghcr.io"
    # Local + history still populated normally.
    assert payload["current_version"] == "2026.4.1"
    assert "2026.4.1" in payload["history"]


def test_list_upgradable_versions_skips_remote_when_disabled(
    temp_clawcu_home, tmp_path
) -> None:
    service, _, openclaw, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    payload = service.list_upgradable_versions("writer", include_remote=False)

    assert payload["remote_requested"] is False
    assert payload["remote_versions"] is None
    assert payload["remote_error"] is None
    # Manager must NOT have been asked at all.
    assert openclaw.list_remote_versions_calls == []


def test_list_service_available_versions_filters_prerelease_tags(
    temp_clawcu_home,
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=[
            "2026.4.10",
            "2026.4.12-beta.1",
            "2026.4.12",
            "2026.4.15-beta.2",
            "2026.4.15",
        ],
        registry="ghcr.io",
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.8", "2026.4.9-rc.1", "2026.4.13"],
        registry="registry-1.docker.io",
    )

    payload = service.list_service_available_versions()

    # Prereleases (anything with a hyphen suffix) are dropped — the
    # `list` footer is an "install candidates" surface, not a tester
    # surface.
    assert payload["openclaw"]["versions"] == [
        "2026.4.10",
        "2026.4.12",
        "2026.4.15",
    ]
    assert payload["hermes"]["versions"] == ["2026.4.8", "2026.4.13"]


def test_list_service_available_versions_skips_when_include_remote_false(
    temp_clawcu_home,
) -> None:
    service, _, openclaw, _ = make_service(temp_clawcu_home)

    payload = service.list_service_available_versions(include_remote=False)

    assert payload["openclaw"]["versions"] is None
    assert payload["hermes"]["versions"] is None
    # No network side effects when disabled.
    assert openclaw.list_remote_versions_calls == []
    assert service.hermes.list_remote_versions_calls == []


def test_list_service_available_versions_caches_fetches_for_the_day(
    temp_clawcu_home,
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.10", "2026.4.15"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.13"], registry="registry-1.docker.io"
    )

    first = service.list_service_available_versions()
    assert first["openclaw"]["versions"] == ["2026.4.10", "2026.4.15"]
    assert first["hermes"]["versions"] == ["2026.4.13"]
    # First call populates the cache with a real network fetch per service.
    assert len(openclaw.list_remote_versions_calls) == 1
    assert len(service.hermes.list_remote_versions_calls) == 1

    cache_path = service._available_versions_cache_path()
    assert cache_path.exists()

    # Swap remote_result to detect whether the second call refetches.
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["9999.9.9"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["9999.9.9"], registry="registry-1.docker.io"
    )

    second = service.list_service_available_versions()
    # Same day → served from cache, no additional manager calls.
    assert len(openclaw.list_remote_versions_calls) == 1
    assert len(service.hermes.list_remote_versions_calls) == 1
    assert second["openclaw"]["versions"] == ["2026.4.10", "2026.4.15"]
    assert second["hermes"]["versions"] == ["2026.4.13"]


def test_list_service_available_versions_refetches_after_day_boundary(
    temp_clawcu_home,
) -> None:
    import json

    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.10"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.13"], registry="registry-1.docker.io"
    )

    service.list_service_available_versions()
    assert len(openclaw.list_remote_versions_calls) == 1
    assert len(service.hermes.list_remote_versions_calls) == 1

    # Simulate yesterday's cache by backdating every entry's fetched_date.
    cache_path = service._available_versions_cache_path()
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    for entry in raw["entries"].values():
        entry["fetched_date"] = "2020-01-01"
    cache_path.write_text(json.dumps(raw), encoding="utf-8")

    # New remote results to confirm the refetch picked up fresh data.
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.20"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.21"], registry="registry-1.docker.io"
    )

    payload = service.list_service_available_versions()
    assert len(openclaw.list_remote_versions_calls) == 2
    assert len(service.hermes.list_remote_versions_calls) == 2
    assert payload["openclaw"]["versions"] == ["2026.4.20"]
    assert payload["hermes"]["versions"] == ["2026.4.21"]


def test_list_service_available_versions_refetches_when_image_repo_changes(
    temp_clawcu_home,
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.10"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.13"], registry="registry-1.docker.io"
    )

    service.list_service_available_versions()
    assert len(openclaw.list_remote_versions_calls) == 1

    # Same day, but the OpenClaw image_repo changed (e.g. via `setup`) — cache
    # entry keyed on the old repo must be invalidated for OpenClaw, while the
    # untouched Hermes entry stays cached.
    openclaw.image_repo = "ghcr.io/mirror/openclaw"
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.99"], registry="ghcr.io"
    )

    payload = service.list_service_available_versions()
    assert len(openclaw.list_remote_versions_calls) == 2
    assert len(service.hermes.list_remote_versions_calls) == 1
    assert payload["openclaw"]["versions"] == ["2026.4.99"]
    assert payload["hermes"]["versions"] == ["2026.4.13"]


def test_list_service_available_versions_does_not_cache_failures(
    temp_clawcu_home,
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, _, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=None, registry="ghcr.io", error="network down"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=None, registry="registry-1.docker.io", error="network down"
    )

    first = service.list_service_available_versions()
    assert first["openclaw"]["error"] == "network down"
    assert first["hermes"]["error"] == "network down"
    # Failed fetches must not leave a stale-success entry on disk.
    assert not service._available_versions_cache_path().exists()

    # Second call must retry rather than serve a cached failure.
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.10"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.13"], registry="registry-1.docker.io"
    )
    payload = service.list_service_available_versions()
    assert len(openclaw.list_remote_versions_calls) == 2
    assert len(service.hermes.list_remote_versions_calls) == 2
    assert payload["openclaw"]["versions"] == ["2026.4.10"]
    assert payload["hermes"]["versions"] == ["2026.4.13"]


def test_list_service_available_versions_falls_back_to_local_on_remote_failure(
    temp_clawcu_home,
) -> None:
    from tests.support import _FakeRemoteTagResult

    service, docker, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=None, registry="ghcr.io", error="Network is unreachable"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=None, registry="registry-1.docker.io", error="Network is unreachable"
    )

    local_by_repo = {
        openclaw.image_repo: ["2026.4.5", "2026.4.8", "latest", "2026.4.12-beta.1"],
        service.hermes.image_repo: ["2026.4.13"],
    }
    docker.list_local_images = lambda repo: list(local_by_repo.get(repo, []))

    payload = service.list_service_available_versions()

    # Remote error is still surfaced (so the user knows why).
    assert payload["openclaw"]["error"] == "Network is unreachable"
    assert payload["hermes"]["error"] == "Network is unreachable"
    # Local images are surfaced as fallback — prereleases and `latest`
    # dropped, sorted oldest -> newest to match the remote contract.
    assert payload["openclaw"]["local_versions"] == ["2026.4.5", "2026.4.8"]
    assert payload["hermes"]["local_versions"] == ["2026.4.13"]


def test_list_service_available_versions_local_fallback_when_remote_disabled(
    temp_clawcu_home,
) -> None:
    service, docker, openclaw, _ = make_service(temp_clawcu_home)
    local_by_repo = {
        openclaw.image_repo: ["2026.4.8"],
        service.hermes.image_repo: [],
    }
    docker.list_local_images = lambda repo: list(local_by_repo.get(repo, []))

    payload = service.list_service_available_versions(include_remote=False)

    assert payload["openclaw"]["versions"] is None
    assert payload["openclaw"]["local_versions"] == ["2026.4.8"]
    assert payload["hermes"]["local_versions"] == []
    # Confirm the registry was NOT contacted.
    assert openclaw.list_remote_versions_calls == []
    assert service.hermes.list_remote_versions_calls == []
    # The CLI renderer uses remote_requested to tell "user opted out of
    # --no-remote" apart from "network failed" so it can dim the opt-out
    # line instead of coloring it yellow like a real failure.
    assert payload["openclaw"]["remote_requested"] is False
    assert payload["hermes"]["remote_requested"] is False


def test_list_service_available_versions_remote_requested_true_on_fetch(
    temp_clawcu_home,
) -> None:
    """Successful fetch and genuine fetch failures both mark
    ``remote_requested=True`` — the CLI uses this flag to render the
    failure line in yellow only when the user actually asked for a fetch.
    """
    from tests.support import _FakeRemoteTagResult

    service, docker, openclaw, _ = make_service(temp_clawcu_home)
    openclaw.remote_result = _FakeRemoteTagResult(
        tags=["2026.4.1"], registry="ghcr.io"
    )
    service.hermes.remote_result = _FakeRemoteTagResult(
        tags=None, error="network unreachable", registry=None
    )
    docker.list_local_images = lambda repo: []

    payload = service.list_service_available_versions()

    assert payload["openclaw"]["remote_requested"] is True
    assert payload["hermes"]["remote_requested"] is True


def test_rollback_restores_snapshot_data_and_instance_env(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    messages: list[str] = []
    service.set_reporter(messages.append)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("v1", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=v1\n", encoding="utf-8")

    upgraded = service.upgrade_instance("writer", version="2026.4.2")
    assert upgraded.version == "2026.4.2"

    (datadir / "state.txt").write_text("v2", encoding="utf-8")
    env_path.write_text("OPENAI_API_KEY=v2\n", encoding="utf-8")

    rolled = service.rollback_instance("writer")

    assert rolled.version == "2026.4.1"
    assert (datadir / "state.txt").read_text(encoding="utf-8") == "v1"
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=v1\n"
    assert any("Upgrade snapshot retained at" in message for message in messages)
    assert any("Restored snapshot" in message for message in messages)
    assert any("Rollback safety snapshot retained at" in message for message in messages)


def test_rollback_plan_returns_preview_without_side_effects(
    temp_clawcu_home, tmp_path
) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("v1", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=18789,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=v1\n", encoding="utf-8")
    service.upgrade_instance("writer", version="2026.4.2")

    commands_before = list(docker.commands)

    plan = service.rollback_plan("writer")

    # Plan must not run any new docker commands.
    assert docker.commands == commands_before
    assert plan["instance"] == "writer"
    assert plan["current_version"] == "2026.4.2"
    assert plan["target_version"] == "2026.4.1"
    assert plan["restore_snapshot"] is not None
    assert plan["restore_snapshot_exists"] is True
    assert plan["selected_action"] == "upgrade"
    assert plan["snapshot_label"] == "rollback-from-2026.4.2"
    assert plan["projected_image"] == "ghcr.io/openclaw/openclaw:2026.4.1"


def test_rollback_plan_with_to_version_targets_specific_event(
    temp_clawcu_home, tmp_path
) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("v1", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.upgrade_instance("writer", version="2026.4.2")
    service.upgrade_instance("writer", version="2026.4.3")

    plan = service.rollback_plan("writer", to_version="2026.4.1")

    assert plan["current_version"] == "2026.4.3"
    assert plan["target_version"] == "2026.4.1"
    assert plan["selected_action"] == "upgrade"


def test_rollback_plan_unknown_target_raises(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.upgrade_instance("writer", version="2026.4.2")

    with pytest.raises(ValueError, match="no rollback snapshot"):
        service.rollback_plan("writer", to_version="2020.1.1")


def test_list_rollback_targets_returns_history_snapshots(
    temp_clawcu_home, tmp_path
) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("v1", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=18789,
        cpu="1",
        memory="2g",
    )
    # No upgrades yet -> empty target list.
    empty = service.list_rollback_targets("writer")
    assert empty["targets"] == []

    service.upgrade_instance("writer", version="2026.4.2")
    service.upgrade_instance("writer", version="2026.4.3")

    payload = service.list_rollback_targets("writer")
    assert payload["current_version"] == "2026.4.3"
    assert len(payload["targets"]) == 2
    assert payload["targets"][0]["action"] == "upgrade"
    assert payload["targets"][0]["restores_to"] == "2026.4.1"
    assert payload["targets"][1]["restores_to"] == "2026.4.2"
    # Snapshots we just produced should exist on disk.
    assert payload["targets"][0]["snapshot_exists"] is True
    assert payload["targets"][1]["snapshot_exists"] is True


def test_rollback_instance_with_to_version_targets_specific_event(
    temp_clawcu_home, tmp_path
) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("v1", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=18789,
        cpu="1",
        memory="2g",
    )
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=v1\n", encoding="utf-8")

    service.upgrade_instance("writer", version="2026.4.2")
    (datadir / "state.txt").write_text("v2", encoding="utf-8")
    env_path.write_text("OPENAI_API_KEY=v2\n", encoding="utf-8")

    service.upgrade_instance("writer", version="2026.4.3")
    (datadir / "state.txt").write_text("v3", encoding="utf-8")
    env_path.write_text("OPENAI_API_KEY=v3\n", encoding="utf-8")

    rolled = service.rollback_instance("writer", to_version="2026.4.1")
    assert rolled.version == "2026.4.1"
    assert (datadir / "state.txt").read_text(encoding="utf-8") == "v1"
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=v1\n"


def test_clone_instance_copies_data_and_starts_new_instance(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    clone = service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
    )

    assert clone.name == "writer-exp"
    assert (Path(clone.datadir) / "memory.txt").read_text(encoding="utf-8") == "hello"
    assert store.load_record("writer-exp").port == 3001


def test_clone_instance_copies_instance_env_file(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text(
        "OPENAI_API_KEY=sk-writer\nOPENAI_BASE_URL=https://api.example.com/v1\n",
        encoding="utf-8",
    )

    service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
    )

    assert store.instance_env_path("writer-exp").read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=sk-writer\nOPENAI_BASE_URL=https://api.example.com/v1\n"
    )


def test_clone_instance_defaults_datadir_and_port_when_not_provided(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port == 18799  # type: ignore[method-assign]
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    clone = service.clone_instance(
        "writer",
        name="writer-exp",
    )

    assert clone.name == "writer-exp"
    assert clone.datadir == str((store.paths.home / "writer-exp").resolve())
    assert (Path(clone.datadir) / "memory.txt").read_text(encoding="utf-8") == "hello"
    assert store.load_record("writer-exp").port == 18799


def test_clone_instance_retries_next_port_when_docker_bind_races(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port in {18799, 18809}  # type: ignore[method-assign]
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    docker.run_errors.append(
        CommandError(
            ["docker", "run"],
            125,
            "",
            "Bind for 0.0.0.0:18799 failed: port is already allocated",
        )
    )

    clone = service.clone_instance(
        "writer",
        name="writer-exp",
    )

    assert clone.port == 18809
    assert ("rm", "clawcu-openclaw-writer-exp") in docker.commands
    stored = store.load_record("writer-exp")
    assert stored.port == 18809
    assert [event["action"] for event in stored.history] == [
        "cloned",
        "create_failed",
        "created",
    ]


def test_clone_instance_rolls_back_target_artifacts_when_start_fails(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text("OPENAI_API_KEY=sk-writer\n", encoding="utf-8")
    docker.fail_next_run = True

    target_dir = tmp_path / "writer-exp"
    with pytest.raises(RuntimeError, match="Failed to create instance 'writer-exp'"):
        service.clone_instance(
            "writer",
            name="writer-exp",
            datadir=str(target_dir),
            port=3001,
        )

    assert not target_dir.exists()
    assert not store.instance_env_path("writer-exp").exists()
    assert not store.instance_path("writer-exp").exists()
    assert ("rm", "clawcu-openclaw-writer-exp") in docker.commands


def test_create_hermes_retries_dashboard_port_with_api_port(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    hermes_adapter = service.adapters["hermes"]
    hermes_adapter._dashboard_ready = lambda _record: True  # type: ignore[method-assign]
    service._is_port_available = lambda port: port in {8652, 9129, 8662, 9139}  # type: ignore[method-assign]
    docker.run_errors.append(
        CommandError(
            ["docker", "run"],
            125,
            "",
            "Bind for 0.0.0.0:9129 failed: port is already allocated",
        )
    )

    created = service.create_hermes(
        name="scribe",
        version="2026.4.13",
        datadir=str(tmp_path / "scribe"),
        cpu="1",
        memory="2g",
    )

    assert created.port == 8662
    assert created.dashboard_port == 9139
    stored = store.load_record("scribe")
    assert stored.port == 8662
    assert stored.dashboard_port == 9139
    assert ("rm", "clawcu-hermes-scribe") in docker.commands
    assert [event["action"] for event in stored.history] == [
        "create_requested",
        "create_failed",
        "created",
    ]


def test_clone_instance_with_version_override_switches_version(
    temp_clawcu_home, tmp_path
) -> None:
    # Explicit --version at clone time lets a user "clone then upgrade"
    # in one step. The clone must run on the new version and the history
    # entry must record both the source version and the target version
    # for provenance.
    service, _, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    clone = service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
        version="2026.4.2",
    )

    assert clone.version == "2026.4.2"
    stored = store.load_record("writer-exp")
    assert stored.version == "2026.4.2"
    cloned_event = next(e for e in stored.history if e["action"] == "cloned")
    assert cloned_event["to_version"] == "2026.4.2"
    assert cloned_event["from_source_version"] == "2026.4.1"
    # Source is untouched.
    assert store.load_record("writer").version == "2026.4.1"


def test_clone_instance_without_version_override_omits_source_version(
    temp_clawcu_home, tmp_path
) -> None:
    # When the clone inherits the source version, `from_source_version`
    # must NOT appear in the history event — it is a marker reserved for
    # clone+upgrade provenance.
    service, _, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
    )

    stored = store.load_record("writer-exp")
    cloned_event = next(e for e in stored.history if e["action"] == "cloned")
    assert cloned_event["to_version"] == "2026.4.1"
    assert "from_source_version" not in cloned_event
    assert cloned_event["secrets_included"] is True


def test_clone_instance_exclude_secrets_skips_env_copy(
    temp_clawcu_home, tmp_path
) -> None:
    # --exclude-secrets translates to include_secrets=False. The source
    # env file must not be propagated to the clone even when it exists
    # at an external path. The history event records the choice.
    service, _, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    store.instance_env_path("writer").write_text(
        "OPENAI_API_KEY=sk-writer\n", encoding="utf-8"
    )

    service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
        include_secrets=False,
    )

    # Env file must NOT be present on the clone.
    assert not store.instance_env_path("writer-exp").exists()
    # History marks the secrets decision so it is auditable later.
    stored = store.load_record("writer-exp")
    cloned_event = next(e for e in stored.history if e["action"] == "cloned")
    assert cloned_event["secrets_included"] is False


def test_clone_instance_reresolves_image_tag_against_current_repo(
    temp_clawcu_home, tmp_path
) -> None:
    """Regression: clone must re-resolve image_tag via prepare_artifact
    rather than inheriting the source record's tag. Users who migrate
    image_repo (e.g. after an upstream registry change) would otherwise
    produce clones pinned to the source's stale repo prefix."""
    service, _, openclaw, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    source_tag = store.load_record("writer").image_tag
    openclaw.image_repo = "registry.example.com/openclaw"
    clone = service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
    )

    assert clone.image_tag == "registry.example.com/openclaw:2026.4.1"
    assert clone.image_tag != source_tag


def test_clone_instance_rejects_existing_target_container(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    docker.status_map["clawcu-openclaw-writer-exp"] = "created"

    with pytest.raises(
        ValueError,
        match="Instance 'writer-exp' already exists. Docker container 'clawcu-openclaw-writer-exp' is already present.",
    ):
        service.clone_instance(
            "writer",
            name="writer-exp",
            datadir=str(tmp_path / "writer-exp"),
            port=3001,
        )
