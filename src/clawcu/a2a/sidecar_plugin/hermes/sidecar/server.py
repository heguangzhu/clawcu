#!/usr/bin/env python3
"""a2a-bridge sidecar — standalone HTTP server binding Hermes to the A2A mesh.

Why a sidecar
-------------
Hermes' plugin system (``hermes_cli.plugins.PluginContext``) exposes
``register_tool``, ``register_hook``, ``register_cli_command`` — it does **not**
expose a way to register HTTP routes on the gateway. The gateway's HTTP
surface (the OpenAI-compat API server on port 8642) is not plugin-extensible.

To satisfy the A2A iter2 hard gate ("curl :9129/.well-known/agent-card.json
returns a real card, POST :9129/a2a/send returns a real Hermes LLM reply"),
this sidecar runs as an independent process inside the Hermes container,
binds the display port, and forwards ``agent_send`` messages to Hermes'
local OpenAI-compat endpoint.

Native-agent routing (iter 4)
-----------------------------
Hermes' ``POST /v1/chat/completions`` is **not** a bare LLM shim. Reading
gateway/platforms/api_server.py::_handle_chat_completions confirms it runs
the full ``AIAgent.run_conversation`` pipeline with SOUL.md persona, enabled
toolsets (``api_server`` platform tools), session DB, fallback provider chain,
and tool-progress callbacks. Hitting that endpoint from this sidecar is
therefore native-agent routing — equivalent in capability to the OpenClaw
sidecar's path through ``gateway /v1/chat/completions`` → ``chat.send``.

Persona injection on Hermes is via ``$HERMES_HOME/SOUL.md`` (see
agent/prompt_builder.py::load_soul_md), mirroring OpenClaw's IDENTITY.md
contract. If SOUL.md is absent the agent falls back to ``DEFAULT_AGENT_IDENTITY``.

Transport summary
-----------------
- Serves ``GET  /.well-known/agent-card.json`` → AgentCard (shared schema).
- Serves ``POST /a2a/send`` with body ``{from, to, message}`` → ``{from, reply}``
  where ``reply`` is the assistant text returned by Hermes'
  ``POST /v1/chat/completions`` on ``127.0.0.1:<HERMES_API_PORT>``.

Stdlib only — ``http.server`` + ``urllib.request``.

Config (env vars, all optional)
-------------------------------
- ``A2A_BIND_HOST``       (default ``0.0.0.0``)
- ``A2A_BIND_PORT``       (default ``9119`` — Hermes display_port inside
                          the javis container; mapped to host 9129)
- ``A2A_SELF_NAME``       (default ``javis``)
- ``A2A_SELF_ROLE``       (default ``Hermes-backed assistant``)
- ``A2A_SELF_SKILLS``     (default ``chat,a2a.bridge``, comma-separated)
- ``A2A_SELF_ENDPOINT``   (default ``http://127.0.0.1:9129/a2a/send``)
- ``HERMES_API_HOST``     (default ``127.0.0.1``)
- ``HERMES_API_PORT``     (default ``8642``)
- ``API_SERVER_KEY``      (required — Hermes' bearer token; same env var
                          Hermes itself reads)
- ``HERMES_MODEL``        (default ``hermes-agent`` — the model id exposed
                          on /v1/models)
- ``A2A_SYSTEM_PROMPT``   (optional; prepended as a system message)
- ``A2A_TIMEOUT_SECONDS`` (default ``300``)

Gateway readiness probe (iter 4, review-5 P1-F)
-----------------------------------------------
The sidecar lazy-probes the Hermes gateway's ``/health`` endpoint from each
``/a2a/send`` so an A2A peer that arrives before the gateway is up gets an
actionable ``503`` instead of a misleading ``502 upstream failed``.

- ``A2A_GATEWAY_READY_DEADLINE_S`` (default ``30`` seconds) — how long the
  sidecar will wait for the gateway to become reachable before giving up.
  Set to ``0`` to force fail-fast (useful for diagnostics). Must be strictly
  less than ``A2A_TIMEOUT_SECONDS``, otherwise readiness eats the upstream
  call's time budget.
- ``A2A_GATEWAY_READY_PROBE_S`` (default ``2``) — per-probe timeout.
- ``A2A_GATEWAY_READY_POLL_S`` (default ``0.5``) — sleep between probe
  attempts inside the deadline window.
- ``A2A_GATEWAY_READY_PATH`` (default ``/health``) — path the sidecar probes
  on the upstream gateway. Injected by the Hermes adapter; kept env-tunable
  so the sidecar itself is gateway-agnostic (openclaw uses ``/healthz``).
- ``A2A_SIDECAR_LOG_DIR`` (no default) — when set, logging is also teed to
  ``<dir>/a2a-sidecar.log`` so the sidecar's audit trail survives
  ``clawcu recreate``. The Hermes adapter points this at the container-side
  ``/opt/data`` mount so the file lands on the host datadir (review-10 P2-C).
- ``A2A_THREAD_DIR`` (no default) — when set, ``/a2a/send`` reads an optional
  ``thread_id`` field from the peer payload and loads prior turns from
  ``<dir>/<peer>/<thread_id>.jsonl`` so the native agent sees continuous
  conversation context. Appended after the reply. Peers that don't send
  ``thread_id`` behave exactly as before. Review-14 P1-C (hermes mirror of
  iter 13's openclaw-side local extension).
- ``A2A_THREAD_MAX_HISTORY_PAIRS`` (default ``10``) — cap on replayed
  user+assistant pairs prepended to ``/v1/chat/completions``. File retains
  all turns. Adapter intentionally does NOT set this in the container env,
  so an instance env-file value wins.

Once a probe succeeds the "ready" observation is cached for 5 minutes. The
cache is dropped explicitly if the next upstream call fails with a socket
error or HTTP 5xx — so a gateway that dies mid-session is detected on the
following request rather than after the TTL (review-4 P1-A).

Run inside the container
------------------------
    docker exec -d clawcu-hermes-javis \\
        env A2A_SELF_NAME=javis python3 /opt/a2a/server.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError

# Make sibling modules importable when this file is run directly as
# `python3 /opt/a2a/server.py` (no package). Done before relative imports.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# Also expose whichever ancestor directory contains _common/. In the baked
# image server.py and _common/ are siblings under /opt/a2a, so _THIS_DIR
# itself satisfies this. When loaded from the source tree during tests
# (hermes/sidecar/server.py) _common/ lives two levels up at
# sidecar_plugin/_common/. Walk up until found.
_probe = _THIS_DIR
for _ in range(4):
    if os.path.isdir(os.path.join(_probe, "_common")):
        if _probe not in sys.path:
            sys.path.insert(0, _probe)
        break
    _parent = os.path.dirname(_probe)
    if _parent == _probe:
        break
    _probe = _parent

from _common.bootstrap import run_bootstrap as run_mcp_bootstrap  # noqa: E402
from _common.outbound_limit import (  # noqa: E402
    create_outbound_limiter,
    create_sweep_thread,
    key_for as outbound_limit_key,
    read_rpm as read_outbound_rpm,
    read_sweep_interval_ms as read_outbound_sweep_interval_ms,
)
from _common.http_response import write_json_response  # noqa: E402
from _common.protocol import (  # noqa: E402
    REQUEST_ID_HEADER,
    read_hop_header,
    read_or_mint_request_id,
)
from _common.mcp import (  # noqa: E402
    ERR_A2A_UPSTREAM as MCP_ERR_A2A_UPSTREAM,
    ERR_INTERNAL as MCP_ERR_INTERNAL,
    ERR_INVALID_PARAMS as MCP_ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST as MCP_ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND as MCP_ERR_METHOD_NOT_FOUND,
    ERR_PARSE as MCP_ERR_PARSE,
    MCP_PROTOCOL_VERSION,
    TOOL_NAME as MCP_TOOL_NAME,
    handle_mcp_request as _shared_handle_mcp_request,
    tool_descriptor as mcp_tool_descriptor,
)
from _common.ratelimit import RateLimiter as PeerRateLimiter  # noqa: E402
from _common import streams as _streams  # noqa: E402
from _common.thread import ThreadStore, safe_id  # noqa: E402


def _setup_logging() -> None:
    """Configure stderr logging plus an optional <datadir>/logs tee.

    Review-10 P2-C: stderr-only logging means ``docker logs`` is the only
    way to see old sidecar output, and those rotate/vanish on
    ``clawcu recreate``. When ``A2A_SIDECAR_LOG_DIR`` is set, we tee to
    ``<dir>/a2a-sidecar.log`` — the Hermes adapter points that at the
    container-side ``/opt/data`` mount so the file lands on the host
    datadir and survives recreate. Append-only; rotation is the
    operator's job (``logrotate``), not the sidecar's.

    File-handler setup is best-effort: a read-only FS or bad path is
    logged to stderr and then ignored. A sidecar that can't open its log
    file must still serve traffic.
    """

    level = os.environ.get("A2A_LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s %(levelname)s a2a-sidecar: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_dir = (os.environ.get("A2A_SIDECAR_LOG_DIR") or "").strip()
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            handlers.append(
                logging.FileHandler(
                    os.path.join(log_dir, "a2a-sidecar.log"),
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            sys.stderr.write(
                f"a2a-sidecar: log-file setup failed, stderr-only: {exc}\n"
            )
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


_setup_logging()
log = logging.getLogger("a2a-sidecar")


# Config + env helpers live in the ``config`` sibling module. Re-imported so
# tests (``mod.Config()``) and test stubs (``server.Config = StubCfg``)
# keep working against the name on this module.
import config  # noqa: E402,F401
from config import Config, _env_list, _envs  # noqa: E402,F401


# Inbound per-peer rate limiter + thread-history store live in _common/
# (imported at the top of this file) — shared with the openclaw sidecar so
# both runtimes share one implementation.


# Gateway readiness lives in its own sibling module. We import the whole
# module (so tests can reach it via ``server.gateway``) and re-export the
# public names so /a2a/send handlers resolve them via server's globals and
# pre-existing test monkey-patches like
#   ``setattr(server, 'wait_for_gateway_ready', stub)``
# continue to work (the cache object is shared by identity, not copied).
import gateway  # noqa: E402,F401
from gateway import (  # noqa: E402
    A2A_LOCAL_UPSTREAM_CAP,
    _GATEWAY_READY_TTL_S,
    _gateway_ready_cache,
    _probe_gateway_ready,
    call_hermes,
    invalidate_gateway_ready_cache,
    wait_for_gateway_ready,
)


# Review-2 P1-D: request correlation.
#
# A single outbound-initiated hop chain (A→B→C) should share one stable ID
# so operators can grep sidecar logs across containers for a single
# federation call. We accept a caller-supplied X-A2A-Request-Id (so higher
# layers can pre-tag) and synthesize uuid4 if absent. The ID is then:
#   * logged at entry + exit on every sidecar that handles it,
#   * forwarded to the next hop via X-A2A-Request-Id,
#   * echoed back in the JSON body AND the response header so both
#     JSON-parsing clients and curl-pipe-grep users can recover it.

_REQUEST_ID_HEADER = REQUEST_ID_HEADER


# --- Outbound helpers ---
#
# The URL allow-list, no-redirect opener, registry lookup, and peer-forward
# helpers live in the ``peering`` sibling module. We import the module (so
# tests can reach symbols via ``server.peering.X``) and re-export the public
# names so call sites and pre-existing tests that use ``mod.lookup_peer`` /
# ``mod.OutboundError`` keep working. Identity — not copy — is preserved:
# ``server._OPENER is peering._OPENER``.
import peering  # noqa: E402,F401
from peering import (  # noqa: E402
    DEFAULT_REGISTRY_URL,
    OutboundError,
    _BadOutboundUrl,
    _NoRedirectHandler,
    _OPENER,
    _OUTBOUND_URL_ALLOWED_SCHEMES,
    _default_registry_url,
    _validate_outbound_url,
    create_peer_cache,
    fetch_peer_list,
    forward_to_peer,
    lookup_peer,
)


# Inbound reject-early guards (body-size cap, content-length validation,
# A2A hop budget) live in the ``inbound_limits`` sibling module. Re-imported
# so pre-existing tests that reach them via ``mod._parse_content_length`` /
# ``mod._BadContentLength`` / ``mod._max_body_bytes`` keep working.
import inbound_limits  # noqa: E402,F401
from inbound_limits import (  # noqa: E402
    DEFAULT_MAX_BODY_BYTES,
    _BadContentLength,
    _BadPayload,
    _hop_budget,
    _max_body_bytes,
    _parse_content_length,
    parse_optional_non_empty_string,
    read_inbound_json_body,
)


def _hop_prelude(
    handler: BaseHTTPRequestHandler,
    *,
    route: str,
) -> tuple[int, str, dict[str, str], bool]:
    """Parse hop + request_id headers; refuse with 508 if the hop budget is spent.

    Shared prelude for ``/a2a/send`` and ``/a2a/outbound``. On refusal, writes
    the 508 response itself (with the caller's ``X-A2A-Request-Id`` echoed
    back) and returns ``refused=True`` so the caller just ``return``s.

    ``route`` is the dot-notation label that appears in the refusal log line
    (``a2a.send``, ``a2a.outbound``) — matches the cadence of the rest of the
    sidecar's log messages so operators can grep uniformly.
    """
    incoming_hop = read_hop_header(handler.headers)
    budget = _hop_budget()
    request_id = read_or_mint_request_id(handler.headers)
    rid_headers = {_REQUEST_ID_HEADER: request_id}
    if incoming_hop >= budget:
        log.warning(
            "%s refused request_id=%s hop=%s budget=%s",
            route,
            request_id,
            incoming_hop,
            budget,
        )
        write_json_response(
            handler,
            508,
            {
                "error": (
                    f"hop budget exceeded (hop={incoming_hop}, budget={budget})"
                ),
                "request_id": request_id,
            },
            extra_headers=rid_headers,
        )
        return incoming_hop, request_id, rid_headers, True
    return incoming_hop, request_id, rid_headers, False


# Response caps (Review-21 P2-M1 / Review-22 P2-N1):
#   A2A_MAX_RESPONSE_BYTES (4 MiB)  — outbound peer/registry responses, owned
#                                    by ``peering`` (a compromised peer is the
#                                    OOM threat)
#   A2A_LOCAL_UPSTREAM_CAP (64 MiB) — co-resident Hermes gateway, owned by
#                                    ``gateway`` (trusted host, still bounded
#                                    against runaway streaming bugs)
# Re-exported here so tests and diagnostic paths can read either value off
# the main sidecar module.
A2A_MAX_RESPONSE_BYTES = peering.A2A_MAX_RESPONSE_BYTES

# The reader + exception live in _common/ so both sidecars share one
# implementation. Aliased locally so pre-existing references to
# ``_read_capped`` / ``_ResponseTooLarge`` keep working without churn.
_ResponseTooLarge = _streams.ResponseTooLarge
_read_capped = _streams.read_capped_bytes


# --- MCP server (a2a-design-3.md §P0-A) ---
#
# The JSON-RPC 2.0 / MCP dispatch lives in ``_common/mcp.py`` and is shared
# with the OpenClaw sidecar. This thin wrapper only defaults the DI
# callbacks to the module-level ``lookup_peer`` / ``forward_to_peer`` so
# the /mcp HTTP route (and tests that rely on module-level stubs) can call
# ``handle_mcp_request`` without re-passing them every time.
def handle_mcp_request(
    body: Any,
    *,
    self_name: str,
    registry_url: str,
    timeout: float,
    request_id: str | None,
    plugin_version: str,
    lookup_peer_fn: Any = None,
    forward_to_peer_fn: Any = None,
    outbound_limiter: Any = None,
    list_peers_fn: Any = None,
    include_role: bool = False,
) -> dict[str, Any]:
    return _shared_handle_mcp_request(
        body,
        self_name=self_name,
        registry_url=registry_url,
        timeout=timeout,
        request_id=request_id,
        plugin_version=plugin_version,
        lookup_peer_fn=lookup_peer_fn or lookup_peer,
        forward_to_peer_fn=forward_to_peer_fn or forward_to_peer,
        outbound_limiter=outbound_limiter,
        outbound_limit_key_fn=outbound_limit_key,
        list_peers_fn=list_peers_fn,
        include_role=include_role,
    )


def build_handler(
    cfg: Config,
    outbound_limiter: Any = None,
    peer_cache: Any = None,
    peer_limiter: PeerRateLimiter | None = None,
) -> type[BaseHTTPRequestHandler]:
    thread_store = ThreadStore(
        storage_dir=cfg.thread_dir,
        max_history_pairs=cfg.thread_max_history_pairs,
    )
    # Review-11 P1-B1: default the inbound per-peer limiter from Config so
    # callers (tests) that inject their own limiter aren't surprised.
    if peer_limiter is None:
        peer_limiter = PeerRateLimiter(per_minute=cfg.rate_limit_per_minute)

    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-bridge-sidecar/1.0"

        def setup(self) -> None:
            # Review-16 P1-H1: cap how long a single inbound request can
            # pin this thread. Applied at the socket layer so both
            # BaseHTTPRequestHandler's header readline and our own
            # rfile.read(length) honor it. ``settimeout(0)`` would mean
            # non-blocking; we want "no limit" semantics via None.
            super().setup()
            to = cfg.inbound_request_timeout_s
            if to > 0:
                try:
                    self.request.settimeout(to)
                except (OSError, AttributeError):
                    pass

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/agent-card.json":
                write_json_response(self, 200, cfg.agent_card())
                return
            if self.path in ("/health", "/healthz"):
                # Accept both spellings so external callers don't need to know
                # which service the sidecar wraps (review-7 P2-E): hermes uses
                # /health internally, openclaw uses /healthz, and the sidecar
                # layer hides that by responding to both.
                write_json_response(
                    self,
                    200,
                    {
                        "status": "ok",
                        "instance": cfg.self_name,
                        "plugin_version": os.environ.get("CLAWCU_PLUGIN_VERSION", "unknown"),
                        # Hermes /v1/chat/completions drives the full AIAgent
                        # pipeline (SOUL.md persona, toolsets, session DB) —
                        # hitting it is native-agent routing, matching the
                        # openclaw sidecar's "mode" marker for parity.
                        "mode": "native-agent",
                        "gateway": f"{cfg.hermes_host}:{cfg.hermes_port}",
                    },
                )
                return
            write_json_response(self, 404, {"error": f"not found: {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/a2a/outbound":
                self._handle_outbound()
                return
            if self.path == "/mcp":
                self._handle_mcp()
                return
            if self.path != "/a2a/send":
                write_json_response(self, 404, {"error": f"not found: {self.path}"})
                return

            # Review-15 P0-A: hop-budget check lives BEFORE body parsing so a
            # runaway loop can't even spend JSON-parse cycles on us.
            incoming_hop, request_id, rid_headers, refused = _hop_prelude(
                self, route="a2a.send"
            )
            if refused:
                return

            payload = read_inbound_json_body(
                self,
                cap=_max_body_bytes(),
                request_id=request_id,
                rid_headers=rid_headers,
            )
            if payload is None:
                return

            peer_from = str(payload.get("from") or "")
            peer_to = str(payload.get("to") or "")
            message = payload.get("message")
            if not isinstance(message, str) or not message:
                write_json_response(
                    self,
                    400,
                    {"error": "`message` must be a non-empty string", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            log.info(
                "a2a.send accepted request_id=%s from=%s hop=%s",
                request_id,
                peer_from or "?",
                incoming_hop,
            )

            # Review-11 P1-B1: per-peer inbound rate limit. Checked after the
            # accepted-log so operators can see the spike in context, but
            # before any gateway call so a flood can't drain the upstream
            # LLM. Mirrors openclaw's ratelimit.js semantics exactly: 429 +
            # Retry-After + resetMs in the body so the peer can back off.
            rl_peer = peer_from or "?"
            rl = peer_limiter.allow(rl_peer)
            if not rl.ok:
                log.warning(
                    "a2a.send rate-limited request_id=%s from=%s reset_ms=%s",
                    request_id,
                    rl_peer,
                    rl.reset_ms,
                )
                write_json_response(
                    self,
                    429,
                    {
                        "error": f"rate limit exceeded for peer '{rl_peer}'",
                        "resetMs": rl.reset_ms,
                        "request_id": request_id,
                    },
                    extra_headers={
                        **rid_headers,
                        "Retry-After": str((rl.reset_ms + 999) // 1000),
                    },
                )
                return

            try:
                thread_id = parse_optional_non_empty_string(payload, "thread_id")
            except _BadPayload as exc:
                write_json_response(
                    self,
                    400,
                    {"error": str(exc), "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return

            if peer_to and peer_to != cfg.self_name:
                # Soft warn; we still answer — federation should route by name, but
                # some callers may mis-address. Preserve reply shape.
                log.warning(
                    "received message addressed to %r but self is %r; answering anyway",
                    peer_to,
                    cfg.self_name,
                )

            if not wait_for_gateway_ready(cfg):
                write_json_response(
                    self,
                    503,
                    {"error": f"gateway not ready after {cfg.ready_deadline}s", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return

            history: list[dict[str, str]] = []
            if thread_id and thread_store.enabled:
                history = thread_store.load_history(peer_from, thread_id)

            try:
                reply = call_hermes(cfg, message, peer_from, history=history)
            except _ResponseTooLarge as e:
                log.error("Hermes response too large request_id=%s: %s", request_id, e)
                write_json_response(
                    self,
                    502,
                    {"error": f"upstream response too large: {e}", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            except HTTPError as e:
                try:
                    body = _read_capped(e, cap=4096).decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                except _ResponseTooLarge:
                    body = f"<upstream error body exceeds 4096 bytes, code={e.code}>"
                log.error("Hermes HTTPError %s request_id=%s: %s", e.code, request_id, body[:500])
                if e.code >= 500:
                    # 5xx suggests gateway is sick; drop the ready-cache so the
                    # next request re-probes instead of blindly retrying.
                    invalidate_gateway_ready_cache()
                write_json_response(
                    self,
                    502,
                    {"error": f"upstream Hermes HTTP {e.code}", "detail": body[:500], "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            except URLError as e:
                log.error("Hermes URLError request_id=%s: %s", request_id, e)
                # Socket failures mean the gateway process is almost certainly
                # gone; invalidate so the sidecar doesn't keep pushing blind
                # for the remainder of the 5-min TTL.
                invalidate_gateway_ready_cache()
                # Review-2 P1-C (iter 3): network-layer (URLError) → 504,
                # distinct from peer HTTP errors above (502). Unifies with
                # /a2a/outbound's forward_to_peer.
                write_json_response(
                    self, 504, {"error": f"upstream Hermes unreachable: {e.reason}", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            except Exception as e:  # noqa: BLE001 — we want to surface anything
                log.exception("unexpected error while calling Hermes request_id=%s", request_id)
                write_json_response(
                    self,
                    500,
                    {"error": f"internal: {e}", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return

            if thread_id and thread_store.enabled:
                thread_store.append_turn(peer_from, thread_id, message, reply)

            log.info("a2a.send replied request_id=%s from=%s", request_id, peer_from or "?")
            write_json_response(
                self,
                200,
                {
                    "from": cfg.self_name,
                    "reply": reply,
                    "thread_id": thread_id,
                    "request_id": request_id,
                },
                extra_headers=rid_headers,
            )

        def _handle_mcp(self) -> None:
            """MCP streamable-http endpoint. See a2a-design-3.md §P0-A.

            Minimal JSON-RPC 2.0 over POST /mcp. Shares request-id with
            /a2a/outbound so an LLM→MCP→peer chain is one grep-able
            transaction. Dispatches to handle_mcp_request, which calls
            forward_to_peer in-process (no second HTTP hop).
            """
            request_id = read_or_mint_request_id(self.headers)
            rid_headers = {_REQUEST_ID_HEADER: request_id}
            cap = _max_body_bytes()
            try:
                length = _parse_content_length(self.headers, cap=cap)
            except _BadContentLength as exc:
                status = 413 if "exceeds" in str(exc) else 400
                write_json_response(
                    self,
                    status,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": MCP_ERR_PARSE,
                            "message": str(exc),
                        },
                    },
                    extra_headers=rid_headers,
                )
                return
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except Exception as exc:  # noqa: BLE001
                write_json_response(
                    self,
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": MCP_ERR_PARSE,
                            "message": f"bad json: {exc}",
                        },
                    },
                    extra_headers=rid_headers,
                )
                return
            log.info(
                "mcp.request request_id=%s method=%s",
                request_id,
                (payload or {}).get("method") if isinstance(payload, dict) else None,
            )
            _list_peers_fn: Any = None
            if (
                peer_cache is not None
                and os.environ.get("A2A_TOOL_DESC_MODE") != "static"
            ):
                _list_peers_fn = peer_cache.get
            response = handle_mcp_request(
                payload,
                self_name=cfg.self_name,
                registry_url=_default_registry_url(),
                timeout=float(cfg.timeout),
                request_id=request_id,
                plugin_version=os.environ.get("CLAWCU_PLUGIN_VERSION", "unknown"),
                outbound_limiter=outbound_limiter,
                list_peers_fn=_list_peers_fn,
                include_role=(
                    os.environ.get("A2A_TOOL_DESC_INCLUDE_ROLE", "").lower() == "true"
                ),
            )
            write_json_response(self, 200, response, extra_headers=rid_headers)

        def _handle_outbound(self) -> None:
            """Container-local outbound primitive. See a2a-design-1.md §Protocol.

            Caller is always inside the same netns (sidecar binds 0.0.0.0 but
            the adapter only publishes 127.0.0.1). Body: {to, message,
            thread_id?, registry_url?, timeout_ms?}. Returns {from, to, reply,
            thread_id, request_id}.
            """
            incoming_hop, request_id, rid_headers, refused = _hop_prelude(
                self, route="a2a.outbound"
            )
            if refused:
                return

            payload = read_inbound_json_body(
                self,
                cap=_max_body_bytes(),
                request_id=request_id,
                rid_headers=rid_headers,
            )
            if payload is None:
                return
            to = payload.get("to")
            if not isinstance(to, str) or not to:
                write_json_response(
                    self,
                    400,
                    {"error": "missing 'to' (string)", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            message = payload.get("message")
            if not isinstance(message, str) or not message:
                write_json_response(
                    self,
                    400,
                    {"error": "'message' must be a non-empty string", "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            try:
                out_thread = parse_optional_non_empty_string(payload, "thread_id")
            except _BadPayload as exc:
                write_json_response(
                    self,
                    400,
                    {"error": str(exc), "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            # Review-17 P1-I1: SSRF guard. The body-level registry_url
            # override lets the caller point the sidecar at any URL for
            # a GET (probe+leak) and, if the attacker controls the
            # response, at any URL for a POST (via card.endpoint). Gate
            # the override behind an operator opt-in; validate scheme
            # even when allowed.
            raw_registry = payload.get("registry_url")
            if raw_registry is not None:
                if not cfg.allow_client_registry_url:
                    write_json_response(
                        self,
                        400,
                        {
                            "error": "client-supplied 'registry_url' is disabled by server policy",
                            "request_id": request_id,
                        },
                        extra_headers=rid_headers,
                    )
                    return
                if not isinstance(raw_registry, str) or not raw_registry:
                    write_json_response(
                        self,
                        400,
                        {
                            "error": "'registry_url' must be a non-empty string when provided",
                            "request_id": request_id,
                        },
                        extra_headers=rid_headers,
                    )
                    return
                try:
                    registry_url = _validate_outbound_url(raw_registry)
                except _BadOutboundUrl as exc:
                    write_json_response(
                        self,
                        400,
                        {
                            "error": f"invalid 'registry_url': {exc}",
                            "request_id": request_id,
                        },
                        extra_headers=rid_headers,
                    )
                    return
            else:
                registry_url = _default_registry_url()
            raw_timeout = payload.get("timeout_ms")
            if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
                timeout_s = float(raw_timeout) / 1000.0
            else:
                timeout_s = 60.0

            # Self-origin rate limit shared with /mcp tool-call path
            # (a2a-design-4.md §P1-B). Checked after input validation so
            # malformed requests aren't counted against the quota.
            if outbound_limiter is not None:
                limit_key = outbound_limit_key(
                    thread_id=out_thread, self_name=cfg.self_name
                )
                decision = outbound_limiter.check(limit_key)
                if not decision.allowed:
                    log.warning(
                        "a2a.outbound rate-limited request_id=%s key=%s limit=%s",
                        request_id,
                        limit_key,
                        decision.limit,
                    )
                    write_json_response(
                        self,
                        429,
                        {
                            "error": f"self-origin rate limit exceeded ({decision.limit}/min)",
                            "request_id": request_id,
                            "retry_after_ms": decision.retry_after_ms,
                        },
                        extra_headers=rid_headers,
                    )
                    return

            log.info(
                "a2a.outbound begin request_id=%s to=%s hop=%s",
                request_id,
                to,
                incoming_hop,
            )
            try:
                card = lookup_peer(registry_url, to, timeout=timeout_s)
            except OutboundError as e:
                log.warning(
                    "a2a.outbound lookup-failed request_id=%s to=%s status=%s",
                    request_id,
                    to,
                    e.http_status,
                )
                write_json_response(
                    self,
                    e.http_status,
                    {"error": str(e), "request_id": request_id},
                    extra_headers=rid_headers,
                )
                return
            try:
                peer_resp = forward_to_peer(
                    endpoint=card["endpoint"],
                    self_name=cfg.self_name,
                    peer_name=to,
                    message=message,
                    thread_id=out_thread,
                    hop=incoming_hop + 1,
                    timeout=timeout_s,
                    request_id=request_id,
                )
            except OutboundError as e:
                log.warning(
                    "a2a.outbound forward-failed request_id=%s to=%s status=%s peer_status=%s",
                    request_id,
                    to,
                    e.http_status,
                    e.peer_status,
                )
                body: dict[str, Any] = {"error": str(e), "request_id": request_id}
                if e.peer_status is not None:
                    body["peer_status"] = e.peer_status
                write_json_response(self, e.http_status, body, extra_headers=rid_headers)
                return

            reply = peer_resp.get("reply") if isinstance(peer_resp, dict) else None
            resp_thread = peer_resp.get("thread_id") if isinstance(peer_resp, dict) else None
            log.info("a2a.outbound done request_id=%s to=%s", request_id, to)
            write_json_response(
                self,
                200,
                {
                    "from": cfg.self_name,
                    "to": to,
                    "reply": reply if isinstance(reply, str) else "",
                    "thread_id": resp_thread if isinstance(resp_thread, str) else out_thread,
                    "request_id": request_id,
                },
                extra_headers=rid_headers,
            )

    return Handler


def main(argv: list[str] | None = None) -> int:
    cfg = Config()

    if not cfg.api_key:
        log.warning(
            "API_SERVER_KEY is empty — Hermes chat calls will likely fail "
            "with 401. Inject the container's env via `docker exec env …` "
            "or `os.environ` before starting this sidecar."
        )

    log.info(
        "starting sidecar: bind=%s:%d self=%s hermes=%s model=%s",
        cfg.bind_host,
        cfg.bind_port,
        cfg.self_name,
        cfg.chat_url(),
        cfg.model,
    )

    # Auto-wire the `a2a` MCP entry into the Hermes service config file on
    # start (a2a-design-4.md §P0-A). Shared impl lives in _common/ so both
    # runtimes use the same bootstrap. Never raises operationally.
    try:
        _env = dict(os.environ)
        _env.setdefault("A2A_BIND_PORT", str(cfg.bind_port))
        _env.setdefault("A2A_SERVICE_MCP_CONFIG_FORMAT", "yaml")
        run_mcp_bootstrap(env=_env)
    except Exception as exc:  # pragma: no cover - defensive path
        log.warning("mcp-bootstrap threw: %s; continuing", exc)

    # Shared outbound rate limiter: same bucket for /a2a/outbound and /mcp
    # tool-call so an LLM can't bypass the cap by picking a different path
    # (a2a-design-4.md §P1-B). The implementation lives in _common/ so both
    # runtimes share the same policy.
    try:
        outbound_limiter = create_outbound_limiter(rpm=read_outbound_rpm(os.environ))
        # a2a-design-6.md §P2-L: periodic empty-bucket sweep so long-running
        # sidecars with high thread_id churn don't grow their hits map
        # unboundedly. Daemon thread; dies with the process. Disable via
        # A2A_OUTBOUND_SWEEP_INTERVAL_MS=0.
        create_sweep_thread(
            outbound_limiter,
            read_outbound_sweep_interval_ms(os.environ),
        )
    except Exception as exc:  # pragma: no cover - defensive path
        log.warning("outbound-limiter init failed: %s; running unthrottled", exc)
        outbound_limiter = None

    # Peer cache for templated tool descriptions (a2a-design-5.md §P1-H).
    # Registry URL doesn't change within a process lifetime in practice, so
    # one cache per sidecar is fine.
    try:
        peer_cache = create_peer_cache(_default_registry_url(), timeout=5.0)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("peer-cache init failed: %s; static tool description only", exc)
        peer_cache = None

    server = ThreadingHTTPServer(
        (cfg.bind_host, cfg.bind_port),
        build_handler(cfg, outbound_limiter=outbound_limiter, peer_cache=peer_cache),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupted — shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
