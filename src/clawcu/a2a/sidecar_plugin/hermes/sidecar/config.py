"""Hermes sidecar runtime configuration.

All env-var parsing lives here so ``server.py`` no longer carries a 75-line
``Config`` wall. The Hermes adapter injects these via container env; this
module owns the defaults, the type coercion (``int``/``float``), and the
clamp-to-sane-values logic.

Tests access the class as ``server.Config()``; the ``server`` module
re-imports the name so that path keeps working and test stubs that replace
``server.Config`` continue to hit the handler code.
"""

from __future__ import annotations

import os
from typing import Any


def _envs(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    if not v:
        return default
    return [s.strip() for s in v.split(",") if s.strip()]


def _env_nonneg_int(name: str, default: int) -> int:
    """Read an ``int`` env var; fall back to ``default`` on empty/malformed/negative.

    Three knobs on this ``Config`` — ``A2A_THREAD_MAX_HISTORY_PAIRS``,
    ``A2A_RATE_LIMIT_PER_MINUTE``, and ``A2A_OUTBOUND_RPM`` (via
    ``_common/outbound_limit``) — share the same "int ≥ 0 or reset to a
    sane default" shape. Collecting it here keeps ``__init__`` linear
    instead of dotted with three-line ``try`` blocks.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class Config:
    def __init__(self) -> None:
        self.bind_host = _envs("A2A_BIND_HOST", "0.0.0.0")
        self.bind_port = int(_envs("A2A_BIND_PORT", "9119"))
        self.self_name = _envs("A2A_SELF_NAME", "javis")
        self.self_role = _envs("A2A_SELF_ROLE", "Hermes-backed assistant")
        self.self_skills = _env_list("A2A_SELF_SKILLS", ["chat", "a2a.bridge"])
        self.self_endpoint = _envs(
            "A2A_SELF_ENDPOINT", "http://127.0.0.1:9129/a2a/send"
        )
        self.hermes_host = _envs("HERMES_API_HOST", "127.0.0.1")
        self.hermes_port = int(_envs("HERMES_API_PORT", "8642"))
        self.api_key = os.environ.get("API_SERVER_KEY") or ""
        self.model = _envs("HERMES_MODEL", "hermes-agent")
        self.system_prompt = os.environ.get("A2A_SYSTEM_PROMPT") or ""
        self.timeout = float(_envs("A2A_TIMEOUT_SECONDS", "300"))
        self.ready_deadline = float(_envs("A2A_GATEWAY_READY_DEADLINE_S", "30"))
        self.ready_probe_timeout = float(_envs("A2A_GATEWAY_READY_PROBE_S", "2"))
        self.ready_poll_interval = float(_envs("A2A_GATEWAY_READY_POLL_S", "0.5"))
        ready_path = _envs("A2A_GATEWAY_READY_PATH", "/health").strip() or "/health"
        self.ready_path = ready_path if ready_path.startswith("/") else f"/{ready_path}"
        # Review-14 P1-C: optional thread-history store. Disabled unless the
        # adapter sets A2A_THREAD_DIR (pointed at a datadir-mounted path).
        self.thread_dir = (os.environ.get("A2A_THREAD_DIR") or "").strip()
        self.thread_max_history_pairs = _env_nonneg_int(
            "A2A_THREAD_MAX_HISTORY_PAIRS", 10
        )
        # Review-11 P1-B1: per-peer sliding-window inbound rate limit on
        # /a2a/send. Mirrors the openclaw sidecar (ratelimit.js) for parity
        # so either service flavor gives peers the same quota guarantee.
        # Units: requests per 60 s. 0 disables. Default 30/min is conservative
        # enough that agent-to-agent chat won't hit it but a runaway loop
        # throttles fast.
        self.rate_limit_per_minute = _env_nonneg_int(
            "A2A_RATE_LIMIT_PER_MINUTE", 30
        )
        # Review-15 P1-H1: bound the time any inbound request can pin a
        # worker thread. Covers both slow-headers (BaseHTTPRequestHandler
        # readline) and slow-body (rfile.read(length)) variants of
        # slowloris. 30 s is well above legitimate client latency (the
        # CLI default send timeout is 60 s end-to-end, but that's the
        # total including gateway+LLM, not the HTTP-layer read). 0 or
        # negative disables (for bench / local debug only).
        try:
            raw_to = float(os.environ.get("A2A_INBOUND_REQUEST_TIMEOUT_S") or "30")
        except ValueError:
            raw_to = 30.0
        self.inbound_request_timeout_s = raw_to if raw_to > 0 else 0.0
        # Review-17 P1-I1: gate the /a2a/outbound body `registry_url`
        # override. Default off — a client cannot pick the registry
        # (SSRF) unless the operator explicitly opts in. Tests that
        # want per-request registry overrides set this flag.
        self.allow_client_registry_url = (
            (os.environ.get("A2A_ALLOW_CLIENT_REGISTRY_URL") or "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on")
        )

    def agent_card(self) -> dict[str, Any]:
        return {
            "name": self.self_name,
            "role": self.self_role,
            "skills": list(self.self_skills),
            "endpoint": self.self_endpoint,
        }

    def chat_url(self) -> str:
        return f"http://{self.hermes_host}:{self.hermes_port}/v1/chat/completions"

    def health_url(self) -> str:
        return f"http://{self.hermes_host}:{self.hermes_port}{self.ready_path}"
