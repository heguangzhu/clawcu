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
