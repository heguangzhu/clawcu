"""Hermes provider registry — vendored from hermes-agent.

Source of truth: hermes-agent/hermes_cli/auth.py::PROVIDER_REGISTRY.
Re-vendor when hermes-agent adds a provider. Tiny mapping by design —
clawcu only needs enough to round-trip provider name ↔ env var ↔
default base URL.

Mirror date: 2026-04-25.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from clawcu.core.provider_models import UnknownProviderError


HermesAuthType = Literal[
    "api_key", "oauth_device_code", "oauth_external", "external_process"
]


@dataclass(frozen=True)
class HermesProviderInfo:
    name: str
    auth_type: HermesAuthType
    api_key_env_var: str | None       # None for OAuth-only providers
    inference_base_url: str | None
    base_url_env_var: str | None      # operator override env var


def _entry(
    name: str,
    auth_type: HermesAuthType,
    api_key_env_var: str | None,
    inference_base_url: str | None,
    base_url_env_var: str | None = None,
) -> tuple[str, HermesProviderInfo]:
    return name, HermesProviderInfo(
        name=name,
        auth_type=auth_type,
        api_key_env_var=api_key_env_var,
        inference_base_url=inference_base_url,
        base_url_env_var=base_url_env_var,
    )


PROVIDER_REGISTRY: dict[str, HermesProviderInfo] = dict([
    _entry("nous",         "oauth_device_code", None,                "https://inference-api.nousresearch.com/v1"),
    _entry("openai-codex", "oauth_external",    None,                "https://chatgpt.com/backend-api/codex"),
    _entry("qwen-oauth",   "oauth_external",    None,                "https://portal.qwen.ai/v1"),
    _entry("copilot",      "api_key",           "COPILOT_GITHUB_TOKEN", "https://api.githubcopilot.com"),
    _entry("copilot-acp",  "external_process",  None,                "acp://copilot",                    "COPILOT_ACP_BASE_URL"),
    _entry("gemini",       "api_key",           "GOOGLE_API_KEY",    "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_BASE_URL"),
    _entry("zai",          "api_key",           "GLM_API_KEY",       "https://api.z.ai/api/paas/v4",     "GLM_BASE_URL"),
    _entry("kimi-coding",  "api_key",           "KIMI_API_KEY",      "https://api.moonshot.ai/v1",       "KIMI_BASE_URL"),
    _entry("minimax",      "api_key",           "MINIMAX_API_KEY",   "https://api.minimax.io/anthropic", "MINIMAX_BASE_URL"),
    _entry("anthropic",    "api_key",           "ANTHROPIC_API_KEY", "https://api.anthropic.com"),
    _entry("alibaba",      "api_key",           "DASHSCOPE_API_KEY", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_BASE_URL"),
    _entry("minimax-cn",   "api_key",           "MINIMAX_CN_API_KEY","https://api.minimaxi.com/anthropic","MINIMAX_CN_BASE_URL"),
    _entry("deepseek",     "api_key",           "DEEPSEEK_API_KEY",  "https://api.deepseek.com/v1",      "DEEPSEEK_BASE_URL"),
    _entry("ai-gateway",   "api_key",           "AI_GATEWAY_API_KEY","https://ai-gateway.vercel.sh/v1",  "AI_GATEWAY_BASE_URL"),
    _entry("opencode-zen", "api_key",           "OPENCODE_ZEN_API_KEY","https://opencode.ai/zen/v1",     "OPENCODE_ZEN_BASE_URL"),
    _entry("opencode-go",  "api_key",           "OPENCODE_GO_API_KEY","https://opencode.ai/zen/go/v1",   "OPENCODE_GO_BASE_URL"),
    _entry("kilocode",     "api_key",           "KILOCODE_API_KEY",  "https://api.kilo.ai/api/gateway",  "KILOCODE_BASE_URL"),
    _entry("huggingface",  "api_key",           "HF_TOKEN",          "https://router.huggingface.co/v1", "HF_BASE_URL"),
])


def info_for(provider_name: str) -> HermesProviderInfo:
    """Return registry entry, deriving a fallback for unknown names.

    Unknown providers get ``auth_type="api_key"`` and an env var derived
    from the provider name (uppercased, non-alphanumerics → ``_``,
    suffix ``_API_KEY``). E.g. ``"my-custom"`` → ``"MY_CUSTOM_API_KEY"``.
    Raises ``UnknownProviderError`` if the name normalizes to empty.
    """
    if provider_name in PROVIDER_REGISTRY:
        return PROVIDER_REGISTRY[provider_name]
    var = re.sub(r"[^A-Za-z0-9]+", "_", provider_name).strip("_").upper()
    if not var:
        raise UnknownProviderError(
            f"Cannot derive env var for provider name {provider_name!r}"
        )
    return HermesProviderInfo(
        name=provider_name,
        auth_type="api_key",
        api_key_env_var=f"{var}_API_KEY",
        inference_base_url=None,
        base_url_env_var=None,
    )
