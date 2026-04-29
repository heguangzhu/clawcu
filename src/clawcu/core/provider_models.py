"""Canonical, service-agnostic provider/model representation.

Used as the in-memory hand-off between an adapter that reads a
collected provider bundle (service-native shape on disk) and an
adapter that writes that provider into a managed instance's datadir.
Lives only between read and write — never serialized.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal


AuthType = Literal["api_key", "oauth"]


@dataclass
class CanonicalModel:
    """Per-model metadata. Most fields are openclaw-rich and Optional;
    hermes only populates ``id`` because hermes' config.yaml just names
    the model — model metadata lives upstream at the provider's API."""
    id: str
    name: str | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    inputs: tuple[str, ...] = ()
    reasoning: bool | None = None
    cost: dict[str, float] | None = None


@dataclass
class CanonicalProvider:
    """A provider rendered into service-agnostic form."""
    name: str
    api_style: str = "openai"
    base_url: str | None = None
    auth_type: AuthType = "api_key"
    api_key: str | None = None
    oauth_blob: str | None = None
    api_key_env_var: str | None = None
    models: tuple[CanonicalModel, ...] = ()
    default_model_id: str | None = None
    fallback_model_ids: tuple[str, ...] = ()
    headers: dict[str, str] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# -- error hierarchy -----------------------------------------------------

class ProviderTranslationError(Exception):
    """Base for all canonical-translation errors. CLI catches and pretty-prints."""


class MissingCredentialError(ProviderTranslationError):
    """Bundle has neither api_key nor oauth_blob."""


class IncompatibleCredentialError(ProviderTranslationError):
    """e.g. Codex OAuth bundle → openclaw destination."""


class UnknownProviderError(ProviderTranslationError):
    """Provider name + fallback env-var derivation both failed."""


# -- override helper -----------------------------------------------------

def apply_overrides(
    canonical: CanonicalProvider,
    *,
    primary: str | None,
    fallbacks: list[str] | None,
) -> CanonicalProvider:
    """Return canonical with primary/fallback overrides applied.

    ``primary`` may be ``"<model-id>"`` or ``"<provider>/<model-id>"`` —
    the slash form also overrides ``canonical.name``. ``fallbacks`` is a
    list of model-ids; empty/whitespace entries are dropped.

    Returns the original instance when no overrides are present (cheap
    no-op), or a fresh instance via ``dataclasses.replace`` otherwise so
    the caller's original canonical is preserved.
    """
    updates: dict[str, Any] = {}
    if primary:
        if "/" in primary:
            provider_name, model_id = primary.split("/", 1)
            if provider_name.strip():
                updates["name"] = provider_name.strip()
            if model_id.strip():
                updates["default_model_id"] = model_id.strip()
        elif primary.strip():
            updates["default_model_id"] = primary.strip()
    if fallbacks:
        cleaned = tuple(f.strip() for f in fallbacks if f and f.strip())
        if cleaned:
            updates["fallback_model_ids"] = cleaned
    return replace(canonical, **updates) if updates else canonical
