"""LLM-driven provider config rendering — optional, best-effort.

When the local ``claude`` CLI is installed and authenticated,
``provider apply --ai`` sends a prompt to Claude and writes the
returned JSON/YAML into the instance datadir.  When ``claude`` is
missing, the CLI surfaces a clear error telling the user to install
it or fall back to the default hard-coded path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

from clawcu.llm import prompts

if TYPE_CHECKING:
    from clawcu.core.provider_models import CanonicalProvider


class LLMRendererError(Exception):
    """Base for all LLM-rendering failures."""


class LLMNotAvailableError(LLMRendererError):
    """Local ``claude`` CLI is not installed or not on PATH."""


class LLMParseError(LLMRendererError):
    """Response could not be parsed as the expected JSON shape."""


def _claude_path() -> str:
    path = shutil.which("claude")
    if path is None:
        raise LLMNotAvailableError(
            "The --ai flag requires the 'claude' CLI. "
            "Install it with: npm install -g @anthropic-ai/claude-code"
        )
    return path


def _call_claude(prompt: str, system_prompt: str) -> str:
    """Invoke the local ``claude -p`` command and return stdout text."""
    cmd = [
        _claude_path(),
        "-p",
        prompt,
        "--system-prompt",
        system_prompt,
        "--no-session-persistence",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise LLMRendererError(
            f"claude CLI exited with code {exc.returncode}. stderr: {stderr}"
        ) from exc
    except FileNotFoundError as exc:
        raise LLMNotAvailableError("claude CLI not found on PATH.") from exc
    return result.stdout


def _extract_json_block(text: str) -> str:
    """Grab the first ```json ... ``` block, or the whole string if none."""
    start = text.find("```json")
    if start == -1:
        start = text.find("```")
    if start != -1:
        end = text.find("```", start + 3)
        if end != -1:
            block = text[start:end].strip()
            if block.startswith("```json"):
                block = block[7:]
            elif block.startswith("```"):
                block = block[3:]
            return block.strip()
    return text.strip()


def _canonical_to_kwargs(canonical: "CanonicalProvider") -> dict[str, str]:
    """Flatten a CanonicalProvider into string placeholders for prompts."""
    models_yaml = ""
    for m in canonical.models:
        models_yaml += f"  - id: {m.id}\n"
        if m.name:
            models_yaml += f"    name: {m.name}\n"
        if m.context_window is not None:
            models_yaml += f"    context_window: {m.context_window}\n"
        if m.max_tokens is not None:
            models_yaml += f"    max_tokens: {m.max_tokens}\n"
    if not models_yaml:
        models_yaml = "  (none)\n"
    return {
        "name": canonical.name,
        "api_style": canonical.api_style,
        "base_url": canonical.base_url or "",
        "auth_type": canonical.auth_type,
        "api_key_env_var": canonical.api_key_env_var or "",
        "default_model_id": canonical.default_model_id or "",
        "fallback_model_ids": ", ".join(canonical.fallback_model_ids),
        "models_yaml": models_yaml,
    }


def render_openclaw(
    canonical: "CanonicalProvider",
    *,
    version_hint: str = "",
) -> dict[str, dict]:
    """Ask Claude to produce openclaw-shaped JSON payloads.

    Returns a dict with ``models_json``, ``auth_profiles_json``,
    ``openclaw_json`` keys.
    """
    prompt = prompts.fill(
        prompts.OPENCLAW_RENDER,
        version_hint=version_hint or "latest",
        **_canonical_to_kwargs(canonical),
    )
    text = _call_claude(
        prompt,
        system_prompt=(
            "You generate precise JSON configuration. "
            "Never add commentary outside the requested JSON block."
        ),
    )
    block = _extract_json_block(text)
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"Claude returned invalid JSON: {exc}") from exc
    for key in ("models_json", "auth_profiles_json", "openclaw_json"):
        if key not in parsed:
            raise LLMParseError(f"Missing '{key}' in Claude response")
    return parsed


def render_hermes(
    canonical: "CanonicalProvider",
    *,
    version_hint: str = "",
) -> dict[str, str]:
    """Ask Claude to produce hermes-shaped YAML + env data.

    Returns a dict with ``config_yaml``, ``env_key``, ``env_value``,
    ``needs_auth_json`` keys.
    """
    prompt = prompts.fill(
        prompts.HERMES_RENDER,
        version_hint=version_hint or "latest",
        **_canonical_to_kwargs(canonical),
    )
    text = _call_claude(
        prompt,
        system_prompt=(
            "You generate precise JSON configuration. "
            "Never add commentary outside the requested JSON block."
        ),
    )
    block = _extract_json_block(text)
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"Claude returned invalid JSON: {exc}") from exc
    for key in ("config_yaml", "env_key", "env_value", "needs_auth_json"):
        if key not in parsed:
            raise LLMParseError(f"Missing '{key}' in Claude response")
    return parsed
