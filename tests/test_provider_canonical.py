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
