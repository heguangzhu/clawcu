"""LLM-assisted provider configuration rendering.

Requires the local ``claude`` CLI (Claude Code) to be installed and
authenticated.  When present, ``provider apply --ai`` invokes
``claude -p`` to generate service-native config from the canonical
provider representation.  Falls back to a clear error when ``claude``
is not on PATH.
"""
from clawcu.llm.prompts import fill
from clawcu.llm.renderer import (
    LLMNotAvailableError,
    LLMParseError,
    LLMRendererError,
    render_hermes,
    render_openclaw,
)

__all__ = [
    "fill",
    "render_openclaw",
    "render_hermes",
    "LLMRendererError",
    "LLMNotAvailableError",
    "LLMParseError",
]
