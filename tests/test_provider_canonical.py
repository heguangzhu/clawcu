"""Tests for the canonical provider/model layer (cross-service apply).

Coverage roadmap (filled in by later tasks):
  - This file: dataclasses + apply_overrides (Task 1).
  - Round-trip (Task 4-7) + cross-service (Task 10).
"""
from __future__ import annotations

import pytest

from clawcu.core.provider_models import (
    CanonicalModel,
    CanonicalProvider,
    IncompatibleCredentialError,
    MissingCredentialError,
    ProviderTranslationError,
    UnknownProviderError,
    apply_overrides,
)


# -- dataclass shape -----------------------------------------------------

def test_canonical_model_has_required_id_and_optional_metadata() -> None:
    m = CanonicalModel(id="k2p5")
    assert m.id == "k2p5"
    assert m.name is None
    assert m.context_window is None
    assert m.max_tokens is None
    assert m.inputs == ()
    assert m.reasoning is None
    assert m.cost is None


def test_canonical_provider_defaults_are_safe() -> None:
    p = CanonicalProvider(name="kimi-coding")
    assert p.api_style == "openai"
    assert p.auth_type == "api_key"
    assert p.api_key is None
    assert p.oauth_blob is None
    assert p.api_key_env_var is None
    assert p.models == ()
    assert p.fallback_model_ids == ()
    assert p.headers is None
    assert p.extras == {}


def test_canonical_provider_carries_full_payload() -> None:
    p = CanonicalProvider(
        name="kimi-coding",
        api_style="anthropic-messages",
        base_url="https://api.kimi.com/coding/",
        api_key="sk-kimi-xyz",
        api_key_env_var="KIMI_API_KEY",
        models=(CanonicalModel(id="k2p5", name="Kimi for Coding", context_window=262144),),
        default_model_id="k2p5",
        headers={"User-Agent": "claude-code/0.1.0"},
        extras={"openclaw_lastGood": {"kimi-coding": "kimi-coding:default"}},
    )
    assert p.models[0].id == "k2p5"
    assert p.headers["User-Agent"] == "claude-code/0.1.0"
    assert p.extras["openclaw_lastGood"]["kimi-coding"] == "kimi-coding:default"


# -- exceptions ---------------------------------------------------------

def test_exception_hierarchy_inherits_from_base() -> None:
    for exc_cls in (MissingCredentialError, IncompatibleCredentialError, UnknownProviderError):
        assert issubclass(exc_cls, ProviderTranslationError)


# -- apply_overrides ----------------------------------------------------

def test_apply_overrides_no_args_returns_same_instance() -> None:
    p = CanonicalProvider(name="x", default_model_id="m1")
    assert apply_overrides(p, primary=None, fallbacks=None) is p


def test_apply_overrides_primary_bare_id_updates_default_only() -> None:
    p = CanonicalProvider(name="kimi-coding", default_model_id="k2p5")
    out = apply_overrides(p, primary="k2p7", fallbacks=None)
    assert out.name == "kimi-coding"
    assert out.default_model_id == "k2p7"


def test_apply_overrides_primary_with_slash_updates_name_and_default() -> None:
    p = CanonicalProvider(name="openrouter", default_model_id="m1")
    out = apply_overrides(p, primary="kimi-coding/k2p5", fallbacks=None)
    assert out.name == "kimi-coding"
    assert out.default_model_id == "k2p5"


def test_apply_overrides_fallbacks_list_becomes_tuple() -> None:
    p = CanonicalProvider(name="x")
    out = apply_overrides(p, primary=None, fallbacks=["m1", "  m2 ", ""])
    assert out.fallback_model_ids == ("m1", "m2")


def test_apply_overrides_returns_new_instance_when_changed() -> None:
    p = CanonicalProvider(name="x", default_model_id="m1")
    out = apply_overrides(p, primary="m2", fallbacks=None)
    assert out is not p
    assert p.default_model_id == "m1"  # original unchanged


# -- hermes provider registry -------------------------------------------

from clawcu.hermes.providers import (  # noqa: E402  (test-grouping)
    HermesProviderInfo,
    PROVIDER_REGISTRY,
    info_for,
)


def test_registry_kimi_coding_curated_entry() -> None:
    info = info_for("kimi-coding")
    assert isinstance(info, HermesProviderInfo)
    assert info.name == "kimi-coding"
    assert info.auth_type == "api_key"
    assert info.api_key_env_var == "KIMI_API_KEY"
    assert info.inference_base_url == "https://api.moonshot.ai/v1"
    assert info.base_url_env_var == "KIMI_BASE_URL"


def test_registry_openai_codex_is_oauth() -> None:
    info = info_for("openai-codex")
    assert info.auth_type == "oauth_external"
    assert info.api_key_env_var is None
    assert info.inference_base_url == "https://chatgpt.com/backend-api/codex"


def test_registry_unknown_provider_derives_env_var() -> None:
    info = info_for("my-custom")
    assert info.auth_type == "api_key"
    assert info.api_key_env_var == "MY_CUSTOM_API_KEY"
    assert info.inference_base_url is None


def test_registry_unknown_with_only_punctuation_raises() -> None:
    with pytest.raises(UnknownProviderError):
        info_for("---")


def test_registry_includes_full_known_set() -> None:
    expected = {
        "nous", "openai-codex", "qwen-oauth", "copilot", "copilot-acp",
        "gemini", "zai", "kimi-coding", "minimax", "anthropic", "alibaba",
        "minimax-cn", "deepseek", "ai-gateway", "opencode-zen",
        "opencode-go", "kilocode", "huggingface",
    }
    assert expected.issubset(set(PROVIDER_REGISTRY))


# -- HermesAdapter.bundle_to_canonical -----------------------------------

import yaml
from clawcu.core.service import ClawCUService
from clawcu.hermes.adapter import HermesAdapter


def _hermes_bundle(*, config_yaml: str, env: str = "", auth_json: str | None = None) -> dict:
    bundle = {
        "service": "hermes",
        "name": "test-provider",
        "metadata": {"service": "hermes", "name": "test-provider"},
        "config_yaml": config_yaml,
        "env": env,
    }
    if auth_json is not None:
        bundle["auth_json"] = auth_json
    return bundle


def test_hermes_to_canonical_api_key_provider(temp_clawcu_home) -> None:
    service = ClawCUService()  # store/docker default OK for this read-only call
    adapter = service.adapters["hermes"]
    bundle = _hermes_bundle(
        config_yaml=yaml.safe_dump({
            "model": {"provider": "kimi-coding", "default": "k2p5"},
        }),
        env="KIMI_API_KEY=sk-kimi-xyz\n",
    )
    bundle["name"] = "kimi-coding"
    bundle["metadata"]["name"] = "kimi-coding"

    canonical = adapter.bundle_to_canonical(service, bundle)

    assert canonical.name == "kimi-coding"
    assert canonical.default_model_id == "k2p5"
    assert canonical.api_key == "sk-kimi-xyz"
    assert canonical.api_key_env_var == "KIMI_API_KEY"
    assert canonical.auth_type == "api_key"
    assert canonical.oauth_blob is None
    assert canonical.base_url == "https://api.moonshot.ai/v1"  # from registry
    # hermes carries no per-model metadata; just an id stub.
    assert len(canonical.models) == 1
    assert canonical.models[0].id == "k2p5"


def test_hermes_to_canonical_explicit_base_url_overrides_registry(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    bundle = _hermes_bundle(
        config_yaml=yaml.safe_dump({
            "model": {
                "provider": "kimi-coding",
                "default": "k2p5",
                "base_url": "https://custom.kimi.example/v1",
            },
        }),
        env="KIMI_API_KEY=sk-kimi-xyz\n",
    )
    bundle["name"] = "kimi-coding"
    canonical = adapter.bundle_to_canonical(service, bundle)
    assert canonical.base_url == "https://custom.kimi.example/v1"


def test_hermes_to_canonical_codex_oauth(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    auth_blob = '{"tokens": {"access_token": "tok-abc"}}'
    bundle = _hermes_bundle(
        config_yaml=yaml.safe_dump({
            "model": {"provider": "openai-codex", "default": "gpt-5.4"},
        }),
        env="",
        auth_json=auth_blob,
    )
    bundle["name"] = "openai-codex"
    canonical = adapter.bundle_to_canonical(service, bundle)
    assert canonical.auth_type == "oauth"
    assert canonical.oauth_blob == auth_blob
    assert canonical.api_key is None


def test_hermes_to_canonical_missing_credential_raises(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    bundle = _hermes_bundle(
        config_yaml=yaml.safe_dump({
            "model": {"provider": "kimi-coding", "default": "k2p5"},
        }),
        env="",  # no key + no auth.json
    )
    bundle["name"] = "kimi-coding"
    with pytest.raises(MissingCredentialError, match="kimi-coding"):
        adapter.bundle_to_canonical(service, bundle)


def test_hermes_to_canonical_fallback_model_translated(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    bundle = _hermes_bundle(
        config_yaml=yaml.safe_dump({
            "model": {"provider": "kimi-coding", "default": "k2p5"},
            "fallback_model": {"provider": "minimax", "model": "MiniMax-M2"},
        }),
        env="KIMI_API_KEY=sk-kimi\n",
    )
    bundle["name"] = "kimi-coding"
    canonical = adapter.bundle_to_canonical(service, bundle)
    assert canonical.fallback_model_ids == ("minimax/MiniMax-M2",)


# -- HermesAdapter.write_canonical ---------------------------------------

import json
from pathlib import Path
from clawcu.core.models import InstanceRecord


def _hermes_record(datadir: Path, name: str = "scribe") -> InstanceRecord:
    return InstanceRecord(
        service="hermes",
        name=name,
        version="2026.4.8",
        upstream_ref="v2026.4.8",
        image_tag="clawcu/hermes-agent:2026.4.8",
        container_name=f"clawcu-hermes-{name}",
        datadir=str(datadir),
        port=8642,
        cpu="1",
        memory="2g",
        auth_mode="native",
        status="running",
        created_at="2026-04-25T00:00:00+00:00",
        updated_at="2026-04-25T00:00:00+00:00",
        history=[],
    )


def test_hermes_write_canonical_api_key_writes_config_and_env(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    datadir = tmp_path / "scribe"
    datadir.mkdir()
    # pre-existing siblings that must be preserved
    (datadir / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: claude-sonnet-4.6\n"
        "smart_model_routing:\n  enabled: false\n"
        "mcp_servers:\n  a2a:\n    url: http://127.0.0.1:9119/mcp\n",
        encoding="utf-8",
    )
    (datadir / ".env").write_text("API_SERVER_KEY=existing-secret\n", encoding="utf-8")

    canonical = CanonicalProvider(
        name="kimi-coding",
        base_url="https://api.moonshot.ai/v1",
        api_key="sk-kimi-xyz",
        api_key_env_var="KIMI_API_KEY",
        default_model_id="k2p5",
        models=(CanonicalModel(id="k2p5"),),
    )

    result = adapter.write_canonical(service, canonical, _hermes_record(datadir))

    config = yaml.safe_load((datadir / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"] == {
        "provider": "kimi-coding",
        "default": "k2p5",
        "base_url": "https://api.moonshot.ai/v1",
    }
    # siblings preserved
    assert config["smart_model_routing"] == {"enabled": False}
    assert config["mcp_servers"] == {"a2a": {"url": "http://127.0.0.1:9119/mcp"}}

    env = (datadir / ".env").read_text(encoding="utf-8")
    assert "KIMI_API_KEY=sk-kimi-xyz" in env
    assert "API_SERVER_KEY=existing-secret" in env  # preserved

    assert result["service"] == "hermes"
    assert result["instance"] == "scribe"
    assert result["env_key"] == "KIMI_API_KEY"


def test_hermes_write_canonical_oauth_writes_auth_json_not_env(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    datadir = tmp_path / "codex"
    datadir.mkdir()
    (datadir / ".env").write_text("API_SERVER_KEY=existing\n", encoding="utf-8")
    canonical = CanonicalProvider(
        name="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_type="oauth",
        oauth_blob='{"tokens": {"access_token": "tok"}}',
        default_model_id="gpt-5.4",
        models=(CanonicalModel(id="gpt-5.4"),),
    )
    adapter.write_canonical(service, canonical, _hermes_record(datadir, "codex"))
    assert (datadir / "auth.json").read_text(encoding="utf-8").startswith('{"tokens"')
    env = (datadir / ".env").read_text(encoding="utf-8")
    assert "API_SERVER_KEY=existing" in env
    assert "OPENAI_API_KEY" not in env


def test_hermes_write_canonical_drops_base_url_when_none(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    datadir = tmp_path / "scribe"
    datadir.mkdir()
    canonical = CanonicalProvider(
        name="my-custom",
        api_key="sk-test",
        api_key_env_var="MY_CUSTOM_API_KEY",
        default_model_id="m1",
        models=(CanonicalModel(id="m1"),),
    )
    adapter.write_canonical(service, canonical, _hermes_record(datadir))
    config = yaml.safe_load((datadir / "config.yaml").read_text(encoding="utf-8"))
    assert "base_url" not in config["model"]


def test_hermes_write_canonical_dry_run_writes_nothing(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    datadir = tmp_path / "scribe"
    datadir.mkdir()
    (datadir / "config.yaml").write_text("model:\n  provider: openrouter\n", encoding="utf-8")
    before_config = (datadir / "config.yaml").read_text(encoding="utf-8")

    canonical = CanonicalProvider(
        name="kimi-coding", api_key="sk-kimi", api_key_env_var="KIMI_API_KEY",
        default_model_id="k2p5", models=(CanonicalModel(id="k2p5"),),
    )
    result = adapter.write_canonical(
        service, canonical, _hermes_record(datadir), dry_run=True,
    )
    after_config = (datadir / "config.yaml").read_text(encoding="utf-8")
    assert before_config == after_config  # untouched
    assert not (datadir / ".env").exists()
    assert result["config_path"].endswith("/config.yaml")
    assert result["env_path"].endswith("/.env")


def test_hermes_write_canonical_with_fallback_model(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["hermes"]
    datadir = tmp_path / "scribe"
    datadir.mkdir()
    canonical = CanonicalProvider(
        name="kimi-coding", api_key="sk-kimi", api_key_env_var="KIMI_API_KEY",
        default_model_id="k2p5",
        fallback_model_ids=("minimax/MiniMax-M2",),
        models=(CanonicalModel(id="k2p5"),),
    )
    adapter.write_canonical(service, canonical, _hermes_record(datadir))
    config = yaml.safe_load((datadir / "config.yaml").read_text(encoding="utf-8"))
    assert config["fallback_model"] == {"provider": "minimax", "model": "MiniMax-M2"}


# -- OpenClawAdapter.bundle_to_canonical ---------------------------------

def _openclaw_bundle(*, provider="kimi-coding", api_key="sk-kimi-test",
                    api="anthropic-messages", base_url="https://api.kimi.com/coding/",
                    models=None) -> dict:
    if models is None:
        models = [{"id": "k2p5", "name": "Kimi for Coding",
                   "contextWindow": 262144, "maxTokens": 32768,
                   "input": ["text", "image"], "reasoning": True,
                   "cost": {"cacheRead": 0, "cacheWrite": 0, "input": 0, "output": 0}}]
    return {
        "service": "openclaw",
        "name": provider,
        "metadata": {"service": "openclaw", "name": provider, "provider": provider,
                     "api_style": api, "endpoint": base_url, "kind": "openclaw-provider"},
        "auth_profiles": {
            "lastGood": {provider: f"{provider}:default"},
            "profiles": {
                f"{provider}:default": {
                    "key": api_key, "provider": provider, "type": "api_key",
                },
            },
        },
        "models": {
            "providers": {
                provider: {
                    "api": api,
                    "apiKey": api_key,
                    "baseUrl": base_url,
                    "headers": {"User-Agent": "claude-code/0.1.0"},
                    "models": models,
                },
            },
        },
    }


def test_openclaw_to_canonical_carries_full_metadata(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["openclaw"]
    canonical = adapter.bundle_to_canonical(service, _openclaw_bundle())
    assert canonical.name == "kimi-coding"
    assert canonical.api_key == "sk-kimi-test"
    assert canonical.api_key_env_var is None  # openclaw doesn't carry env vars
    assert canonical.api_style == "anthropic-messages"
    assert canonical.base_url == "https://api.kimi.com/coding/"
    assert canonical.headers == {"User-Agent": "claude-code/0.1.0"}
    assert canonical.default_model_id == "k2p5"
    assert canonical.models[0].id == "k2p5"
    assert canonical.models[0].name == "Kimi for Coding"
    assert canonical.models[0].context_window == 262144
    assert canonical.models[0].max_tokens == 32768
    assert canonical.models[0].inputs == ("text", "image")
    assert canonical.models[0].reasoning is True
    assert canonical.models[0].cost == {"cacheRead": 0, "cacheWrite": 0, "input": 0, "output": 0}
    assert canonical.extras["openclaw_lastGood"] == {"kimi-coding": "kimi-coding:default"}


def test_openclaw_to_canonical_missing_api_key_raises(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["openclaw"]
    bundle = _openclaw_bundle(api_key="")
    with pytest.raises(MissingCredentialError):
        adapter.bundle_to_canonical(service, bundle)


def test_openclaw_to_canonical_default_api_style_when_missing(temp_clawcu_home) -> None:
    service = ClawCUService()
    adapter = service.adapters["openclaw"]
    bundle = _openclaw_bundle(api="")
    canonical = adapter.bundle_to_canonical(service, bundle)
    assert canonical.api_style == "openai"


# -- OpenClawAdapter.write_canonical -------------------------------------

def _openclaw_record(datadir: Path, name: str = "writer") -> InstanceRecord:
    return InstanceRecord(
        service="openclaw",
        name=name,
        version="2026.4.1",
        upstream_ref="v2026.4.1",
        image_tag="clawcu/openclaw:2026.4.1",
        container_name=f"clawcu-openclaw-{name}",
        datadir=str(datadir),
        port=18799,
        cpu="1",
        memory="2g",
        auth_mode="token",
        status="running",
        created_at="2026-04-25T00:00:00+00:00",
        updated_at="2026-04-25T00:00:00+00:00",
        history=[],
    )


def test_openclaw_write_canonical_writes_runtime_files(temp_clawcu_home, tmp_path) -> None:
    from clawcu.core.storage import StateStore
    from clawcu.core.docker import DockerManager
    # need a real store for service.store.append_log + env helpers
    service = ClawCUService(store=StateStore(), docker=DockerManager())
    adapter = service.adapters["openclaw"]
    datadir = tmp_path / "writer"
    canonical = CanonicalProvider(
        name="kimi-coding", api_style="anthropic-messages",
        base_url="https://api.kimi.com/coding/",
        api_key="sk-kimi-xyz",
        headers={"User-Agent": "claude-code/0.1.0"},
        default_model_id="k2p5",
        models=(CanonicalModel(
            id="k2p5", name="Kimi for Coding", context_window=262144,
            max_tokens=32768, inputs=("text", "image"), reasoning=True,
            cost={"cacheRead": 0, "cacheWrite": 0, "input": 0, "output": 0},
        ),),
    )
    record = _openclaw_record(datadir)

    adapter.write_canonical(service, canonical, record, agent="main")

    runtime_dir = datadir / "agents" / "main" / "agent"
    auth = json.loads((runtime_dir / "auth-profiles.json").read_text(encoding="utf-8"))
    models = json.loads((runtime_dir / "models.json").read_text(encoding="utf-8"))
    config = json.loads((datadir / "openclaw.json").read_text(encoding="utf-8"))

    assert "kimi-coding:default" in auth["profiles"]
    assert auth["profiles"]["kimi-coding:default"]["key"] == "sk-kimi-xyz"
    assert auth["lastGood"] == {"kimi-coding": "kimi-coding:default"}

    p = models["providers"]["kimi-coding"]
    assert p["api"] == "anthropic-messages"
    assert p["baseUrl"] == "https://api.kimi.com/coding/"
    assert p["headers"] == {"User-Agent": "claude-code/0.1.0"}
    assert p["models"][0]["id"] == "k2p5"
    assert p["models"][0]["contextWindow"] == 262144

    assert "kimi-coding" in config["models"]["providers"]


def test_openclaw_write_canonical_oauth_raises(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["openclaw"]
    datadir = tmp_path / "writer"
    canonical = CanonicalProvider(
        name="openai-codex",
        auth_type="oauth", oauth_blob='{"tokens": {}}',
        default_model_id="gpt-5.4",
        models=(CanonicalModel(id="gpt-5.4"),),
    )
    with pytest.raises(IncompatibleCredentialError, match="openai-codex"):
        adapter.write_canonical(service, canonical, _openclaw_record(datadir))


def test_openclaw_write_canonical_synthesizes_zero_defaults(temp_clawcu_home, tmp_path) -> None:
    """When canonical.models has only id (e.g. from a hermes source),
    openclaw write must fill zeros rather than emit broken model objects."""
    from clawcu.core.storage import StateStore
    from clawcu.core.docker import DockerManager
    service = ClawCUService(store=StateStore(), docker=DockerManager())
    adapter = service.adapters["openclaw"]
    datadir = tmp_path / "writer"
    canonical = CanonicalProvider(
        name="kimi-coding", api_style="openai", api_key="sk-kimi",
        base_url="https://api.moonshot.ai/v1",
        default_model_id="k2p5",
        models=(CanonicalModel(id="k2p5"),),  # bare-id only
    )
    adapter.write_canonical(service, canonical, _openclaw_record(datadir))
    models = json.loads((datadir / "agents" / "main" / "agent" / "models.json").read_text(encoding="utf-8"))
    m = models["providers"]["kimi-coding"]["models"][0]
    assert m["id"] == "k2p5"
    assert m["name"] == "k2p5"  # falls back to id when canonical.name absent
    assert m["contextWindow"] == 0
    assert m["maxTokens"] == 0
    assert m["input"] == ["text"]
    assert m["reasoning"] is False
    assert m["cost"] == {"cacheRead": 0, "cacheWrite": 0, "input": 0, "output": 0}


def test_openclaw_write_canonical_dry_run_writes_nothing(temp_clawcu_home, tmp_path) -> None:
    service = ClawCUService()
    adapter = service.adapters["openclaw"]
    datadir = tmp_path / "writer"
    canonical = CanonicalProvider(
        name="kimi-coding", api_key="sk-kimi", default_model_id="k2p5",
        models=(CanonicalModel(id="k2p5"),),
    )
    result = adapter.write_canonical(service, canonical, _openclaw_record(datadir), dry_run=True)
    assert not (datadir / "agents").exists()
    assert not (datadir / "openclaw.json").exists()
    assert "writes" in result
    assert any("auth-profiles.json" in path for path in result["writes"])
    assert any("models.json" in path for path in result["writes"])
    assert any("openclaw.json" in path for path in result["writes"])


# -- plan_apply_provider through canonical --------------------------------

def test_plan_apply_provider_returns_dst_planned_writes(temp_clawcu_home, tmp_path) -> None:
    """plan_apply_provider must dispatch to dst_adapter.write_canonical
    with dry_run=True so it works for cross-service plans too."""
    # smoke: build a service with a real store, save a hermes record, save
    # an openclaw bundle for kimi-coding, then plan_apply_provider should
    # return a hermes-shaped plan (config_path/env_path), not openclaw.
    from clawcu.core.storage import StateStore
    from clawcu.core.docker import DockerManager
    from clawcu.core.models import InstanceRecord
    service = ClawCUService(store=StateStore(), docker=DockerManager())
    # save a hermes target instance record
    target = tmp_path / "scribe"
    target.mkdir()
    service.store.save_record(InstanceRecord(
        service="hermes", name="scribe", version="2026.4.8",
        upstream_ref="v2026.4.8", image_tag="clawcu/hermes:test",
        container_name="clawcu-hermes-scribe", datadir=str(target),
        port=8642, cpu="1", memory="2g", auth_mode="native", status="running",
        created_at="2026-04-25T00:00:00+00:00",
        updated_at="2026-04-25T00:00:00+00:00", history=[],
    ))
    # save an openclaw bundle for kimi-coding
    service.store.save_provider_bundle("openclaw", "kimi-coding", _openclaw_bundle())

    plan = service.plan_apply_provider("kimi-coding", "scribe")
    assert plan["service"] == "hermes"
    assert plan["instance"] == "scribe"
    assert plan["config_path"].endswith("/config.yaml")
    assert plan["env_path"].endswith("/.env")
    # nothing on disk
    assert not (target / "config.yaml").exists()
    assert not (target / ".env").exists()
