#!/usr/bin/env python3
"""a2a-bridge sidecar for OpenClaw instances (Python port of server.js).

Architecture (iter3 — native-agent routing):
  The sidecar forwards /a2a/send to the gateway's own OpenAI-compatible
  endpoint at /v1/chat/completions. That endpoint is handled by
  gateway/server-methods/chat.ts via chat.send, which runs the full
  OpenClaw agent turn (persona, skills, tools, provider) — so an A2A peer
  gets the agent's "native" reply, not a bare LLM completion.

Authentication:
  Gateway runs with auth.mode=token; the token lives in openclaw.json →
  gateway.auth.token. The sidecar reads that file at request time and
  sends Authorization: Bearer <token>.

Usage:
  python3 server.py --local --port 18790 [--name <instance>]
  python3 server.py --instance <name> --port 18820 [--container <name>]
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import traceback
import uuid
from typing import Any, Dict, Optional, Tuple

# Make sibling modules importable when this file is run directly as
# `python3 /opt/a2a/server.py` (no package). Done before relative imports.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# Also expose whichever ancestor directory contains _common/. In the baked
# image server.py and _common/ are siblings under /opt/a2a, so _THIS_DIR
# itself satisfies this. When loaded from the source tree during tests
# (openclaw/sidecar/server.py) _common/ lives two levels up at
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

from logsink import default_logger, setup_file_log  # noqa: E402
from readiness import (  # noqa: E402
    invalidate_gateway_ready,
    looks_like_gateway_down,
    wait_for_gateway_ready,
)
from _common.bootstrap import run_bootstrap as run_mcp_bootstrap  # noqa: E402
from _common.http_response import write_json_response  # noqa: E402
from _common.mcp import (  # noqa: E402
    ERR_PARSE as MCP_ERR_PARSE,
    UpstreamError,
    handle_mcp_request,
    is_tool_desc_static,
    json_rpc_error,
    tool_desc_include_role,
    write_upstream_error_response,
)
from _common.outbound_limit import (  # noqa: E402
    create_outbound_limiter,
    create_sweep_timer,
    key_for as outbound_limit_key,
    read_rpm as read_outbound_rpm,
    read_sweep_interval_ms as read_outbound_sweep_interval_ms,
    write_outbound_rate_limit_response,
)
from _common.protocol import (  # noqa: E402
    REQUEST_ID_HEADER,
    hop_budget_from_env,
    hop_prelude as _shared_hop_prelude,
    read_hop_header,
    read_or_mint_request_id,
    write_error_envelope,
    write_outbound_reply_response,
    write_send_reply_response,
)
from _common.ratelimit import (  # noqa: E402
    create_rate_limiter,
    write_peer_rate_limit_response,
)
from _common.thread import create_thread_store  # noqa: E402
from adapters import (  # noqa: E402
    HostAdapter,
    LocalAdapter,
    OPENCLAW_AUTH_PATH,
    OPENCLAW_CONFIG_PATH,
    make_host_adapter,
    make_local_adapter,
    read_gateway_auth,
)
from http_client import (  # noqa: E402
    A2A_MAX_RESPONSE_BYTES,
    ResponseTooLarge,
    _http_call,
    _read_capped,
    http_request_raw,
    parse_http_url,
    post_json,
)
from outbound import (  # noqa: E402
    _default_now_ms,
    create_peer_cache,
    default_registry_url,
    fetch_peer_list,
    forward_to_peer,
    lookup_peer,
    read_allow_client_registry_url,
)
from chat import (  # noqa: E402
    build_a2a_context,
    post_chat_completion,
)
from _common.inbound_limits import (  # noqa: E402
    _max_body_bytes,
    mcp_prelude,
    read_inbound_json_body,
    read_inbound_mcp_body,
)
from _common.payload import (  # noqa: E402
    BadPayload,
    parse_optional_non_empty_string,
    require_non_empty_string,
    write_bad_payload_response,
)

__all__ = [
    # Re-exported for tests and for any caller that used to import these
    # directly from server.py before the sidecar/* splits.
    "A2A_MAX_RESPONSE_BYTES",
    "HostAdapter",
    "LocalAdapter",
    "OPENCLAW_AUTH_PATH",
    "OPENCLAW_CONFIG_PATH",
    "ResponseTooLarge",
    "build_a2a_context",
    "create_peer_cache",
    "fetch_peer_list",
    "forward_to_peer",
    "http_request_raw",
    "lookup_peer",
    "make_host_adapter",
    "make_local_adapter",
    "parse_http_url",
    "post_chat_completion",
    "post_json",
    "read_allow_client_registry_url",
    "read_gateway_auth",
]

# Hop budget read at module-scope so tests can import it without running main().
# The parser + default live in _common.protocol so hermes and openclaw share one
# implementation.
A2A_HOP_BUDGET = hop_budget_from_env()

# Module-level outbound limiter — handlers close over it.
OUTBOUND_LIMITER = create_outbound_limiter(rpm=read_outbound_rpm(os.environ))

# File-tee for logs (opt-in via A2A_SIDECAR_LOG_DIR). Done at import time so
# even setup-phase logs land in the file. Safe no-op when env var unset.
setup_file_log(os.environ.get("A2A_SIDECAR_LOG_DIR") or "")


def parse_args(argv) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if not a.startswith("--"):
            i += 1
            continue
        key = a[2:]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if nxt is None or nxt.startswith("--"):
            out[key] = True
            i += 1
        else:
            out[key] = nxt
            i += 2
    return out


def _int_from_sources(*sources: Any, default: int) -> int:
    """Pick the first truthy source, parse it as ``int``, fall back to ``default``.

    ``main()`` reads half a dozen integer knobs that each accept an
    argv override first, then one or more env-var fallbacks, then a
    literal default — the same "try each, int-parse, tolerate a bad
    value by reverting to the hardcoded fallback" loop inlined six
    times. Collecting it here keeps ``main()`` linear and makes each
    knob's source ordering immediately scannable instead of buried
    under a try/except wrapper.
    """
    for src in sources:
        if src:
            try:
                return int(src)
            except (TypeError, ValueError):
                return default
    return default


# HTTP helpers live in http_client.py. Re-imported above.


def _resolve_outbound_registry_url(
    handler: Any,
    *,
    payload: Dict[str, Any],
    request_id: str,
    rid_headers: Dict[str, str],
) -> Optional[str]:
    """Pick the registry URL for ``/a2a/outbound``, enforcing operator policy.

    When the peer omits ``registry_url`` the sidecar falls back to the
    env-driven default. When the peer supplies one, the operator must
    have opted in via ``A2A_ALLOW_CLIENT_REGISTRY_URL`` and the value
    must be a non-empty string. On any policy/shape failure this writes
    the uniform 400 response itself and returns ``None`` so the caller's
    next line is just ``return``. Mirrors hermes's
    ``_resolve_outbound_registry_url`` (minus SSRF scheme validation,
    which is hermes-local — openclaw's peers are trusted containers).
    """
    if "registry_url" not in payload:
        return default_registry_url(os.environ)

    def _reject(msg: str) -> None:
        write_error_envelope(
            handler, 400, msg, request_id=request_id, rid_headers=rid_headers
        )

    if not read_allow_client_registry_url(os.environ):
        _reject("client-supplied 'registry_url' is disabled by server policy")
        return None
    raw = payload.get("registry_url")
    if not isinstance(raw, str) or not raw:
        _reject("'registry_url' must be a non-empty string when provided")
        return None
    return raw


# ---- HTTP server ------------------------------------------------------------

class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler_class(ctx: Dict[str, Any]):
    logger = ctx["logger"]
    self_name = ctx["self_name"]
    card = ctx["card"]
    adapter = ctx["adapter"]
    gateway_host = ctx["gateway_host"]
    gateway_port = ctx["gateway_port"]
    gateway_ready_path = ctx["gateway_ready_path"]
    gateway_ready_deadline_ms = ctx["gateway_ready_deadline_ms"]
    request_timeout_ms = ctx["request_timeout_ms"]
    model = ctx["model"]
    rate_limiter = ctx["rate_limiter"]
    thread_store = ctx["thread_store"]
    # Shared, lazily-initialised peer cache.
    peer_cache_holder: Dict[str, Any] = {"cache": None, "lock": threading.Lock()}

    def _ensure_peer_cache(registry_url: str):
        with peer_cache_holder["lock"]:
            if peer_cache_holder["cache"] is None:
                peer_cache_holder["cache"] = create_peer_cache(registry_url=registry_url, timeout_ms=5000)
            return peer_cache_holder["cache"]

    def _on_hop_refused(route: str, request_id: str, hop: int, budget: int) -> None:
        logger.warn(
            f"[sidecar:{self_name}] {route} refused request_id={request_id} hop={hop} budget={budget}"
        )

    def _hop_prelude(handler, *, route: str) -> Tuple[int, str, Dict[str, str], bool]:
        return _shared_hop_prelude(
            handler,
            route=route,
            budget=A2A_HOP_BUDGET,
            on_refused=_on_hop_refused,
        )

    class Handler(http.server.BaseHTTPRequestHandler):
        # Silence BaseHTTPRequestHandler's stderr default — we log ourselves.
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        # ---- GET --------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            try:
                path = (self.path or "/").split("?", 1)[0]
                if path == "/.well-known/agent-card.json":
                    return write_json_response(self, 200, card)
                if path in ("/health", "/healthz"):
                    return write_json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "instance": self_name,
                            "plugin_version": os.environ.get("CLAWCU_PLUGIN_VERSION") or "unknown",
                            "mode": "native-agent",
                            "gateway": f"{gateway_host}:{gateway_port}",
                        },
                    )
                return write_json_response(self, 404, {"error": "not found"})
            except Exception as err:
                logger.error(f"[sidecar:{self_name}] unhandled:", traceback.format_exc() or err)
                try:
                    write_json_response(self, 500, {"error": "internal error"})
                except Exception:
                    pass

        # ---- POST -------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            try:
                path = (self.path or "/").split("?", 1)[0]
                if path == "/a2a/send":
                    return self._handle_a2a_send()
                if path == "/a2a/outbound":
                    return self._handle_a2a_outbound()
                if path == "/mcp":
                    return self._handle_mcp()
                return write_json_response(self, 404, {"error": "not found"})
            except Exception as err:
                logger.error(f"[sidecar:{self_name}] unhandled:", traceback.format_exc() or err)
                try:
                    write_json_response(self, 500, {"error": "internal error"})
                except Exception:
                    pass

        # ---- /a2a/send -------------------------------------------------------

        def _handle_a2a_send(self) -> None:
            incoming_hop, request_id, rid_headers, refused = _hop_prelude(self, route="a2a.send")
            if refused:
                return
            body = read_inbound_json_body(
                self,
                cap=_max_body_bytes(),
                request_id=request_id,
                rid_headers=rid_headers,
            )
            if body is None:
                return
            try:
                message = require_non_empty_string(body, "message")
                peer_from = require_non_empty_string(body, "from")
                thread_id = parse_optional_non_empty_string(body, "thread_id")
            except BadPayload as exc:
                return write_bad_payload_response(
                    self, exc, request_id=request_id, rid_headers=rid_headers
                )

            logger.info(
                f"[sidecar:{self_name}] a2a.send accepted request_id={request_id} from={peer_from} hop={incoming_hop}"
            )

            rl = rate_limiter.allow(peer_from)
            if not rl.ok:
                return write_peer_rate_limit_response(
                    self,
                    rl,
                    peer=peer_from,
                    request_id=request_id,
                    rid_headers=rid_headers,
                )

            try:
                auth = read_gateway_auth(adapter)
            except Exception as exc:
                return write_error_envelope(
                    self, 503, f"instance not ready: {exc}",
                    request_id=request_id, rid_headers=rid_headers,
                )

            ready = wait_for_gateway_ready(
                host=gateway_host,
                port=gateway_port,
                path=gateway_ready_path,
                deadline_ms=gateway_ready_deadline_ms,
            )
            if not ready:
                return write_error_envelope(
                    self, 503,
                    f"gateway not ready after {gateway_ready_deadline_ms}ms",
                    request_id=request_id, rid_headers=rid_headers,
                )

            history = thread_store.load_history(peer_from, thread_id)

            try:
                reply = post_chat_completion(
                    gateway_host=gateway_host,
                    gateway_port=gateway_port,
                    token=auth.get("token"),
                    user_message=message,
                    system_prompt=build_a2a_context(self_name, peer_from),
                    history=history,
                    model=model,
                    timeout_ms=request_timeout_ms,
                )
            except Exception as exc:
                logger.error(
                    f"[sidecar:{self_name}] gateway call failed request_id={request_id}: {exc}"
                )
                if looks_like_gateway_down(exc):
                    invalidate_gateway_ready()
                return write_error_envelope(
                    self, 502, f"upstream agent failed: {exc}",
                    request_id=request_id, rid_headers=rid_headers,
                )

            thread_store.append_turn(peer_from, thread_id, message, reply)

            logger.info(
                f"[sidecar:{self_name}] a2a.send replied request_id={request_id} from={peer_from}"
            )
            return write_send_reply_response(
                self,
                self_name=self_name,
                reply=reply,
                thread_id=thread_id,
                request_id=request_id,
                rid_headers=rid_headers,
            )

        # ---- /a2a/outbound ---------------------------------------------------

        def _handle_a2a_outbound(self) -> None:
            incoming_hop, request_id, rid_headers, refused = _hop_prelude(self, route="a2a.outbound")
            if refused:
                return
            body = read_inbound_json_body(
                self,
                cap=_max_body_bytes(),
                request_id=request_id,
                rid_headers=rid_headers,
            )
            if body is None:
                return
            try:
                to = require_non_empty_string(body, "to")
                message = require_non_empty_string(body, "message")
                out_thread_id = parse_optional_non_empty_string(body, "thread_id")
            except BadPayload as exc:
                return write_bad_payload_response(
                    self, exc, request_id=request_id, rid_headers=rid_headers
                )

            limit_key = outbound_limit_key(thread_id=out_thread_id, self_name=self_name)
            limit = OUTBOUND_LIMITER.check(limit_key)
            if not limit.allowed:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound self-rate-limited request_id={request_id} key={limit_key} limit={limit.limit}"
                )
                return write_outbound_rate_limit_response(
                    self, limit, request_id=request_id, rid_headers=rid_headers
                )

            registry_url = _resolve_outbound_registry_url(
                self, payload=body, request_id=request_id, rid_headers=rid_headers
            )
            if registry_url is None:
                return

            try:
                timeout_ms_num = float(body.get("timeout_ms"))
            except (TypeError, ValueError):
                timeout_ms_num = float("nan")
            timeout_ms = int(timeout_ms_num) if timeout_ms_num == timeout_ms_num and timeout_ms_num > 0 else 60000

            logger.info(
                f"[sidecar:{self_name}] a2a.outbound begin request_id={request_id} to={to} hop={incoming_hop}"
            )

            try:
                card_resp = lookup_peer(
                    registry_url=registry_url, peer_name=to, timeout_ms=timeout_ms
                )
            except UpstreamError as exc:
                status = exc.http_status or 503
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound lookup-failed request_id={request_id} to={to} status={status}"
                )
                return write_upstream_error_response(
                    self, exc, request_id=request_id, rid_headers=rid_headers, default_status=503
                )
            except Exception as exc:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound lookup-failed request_id={request_id} to={to} status=503"
                )
                return write_error_envelope(
                    self, 503, str(exc), request_id=request_id, rid_headers=rid_headers
                )

            try:
                peer_resp = forward_to_peer(
                    endpoint=card_resp["endpoint"],
                    self_name=self_name,
                    peer_name=to,
                    message=message,
                    thread_id=out_thread_id,
                    hop=incoming_hop + 1,
                    timeout_ms=timeout_ms,
                    request_id=request_id,
                )
            except UpstreamError as exc:
                status = exc.http_status or 502
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound forward-failed request_id={request_id} to={to} "
                    f"status={status} peer_status={exc.peer_status if exc.peer_status is not None else '-'}"
                )
                return write_upstream_error_response(
                    self, exc, request_id=request_id, rid_headers=rid_headers, default_status=502
                )
            except Exception as exc:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound forward-failed request_id={request_id} to={to} status=502"
                )
                return write_error_envelope(
                    self, 502, str(exc), request_id=request_id, rid_headers=rid_headers
                )

            logger.info(
                f"[sidecar:{self_name}] a2a.outbound done request_id={request_id} to={to}"
            )
            return write_outbound_reply_response(
                self,
                self_name=self_name,
                to=to,
                peer_resp=peer_resp,
                fallback_thread_id=out_thread_id,
                request_id=request_id,
                rid_headers=rid_headers,
            )

        # ---- /mcp ------------------------------------------------------------

        def _handle_mcp(self) -> None:
            request_id, rid_headers, body, ok = mcp_prelude(
                self, cap=_max_body_bytes()
            )
            if not ok:
                return
            registry_url = default_registry_url(os.environ)
            method = body.get("method") if isinstance(body, dict) else None
            logger.info(f"[sidecar:{self_name}] mcp.request request_id={request_id} method={method}")
            list_peers = None if is_tool_desc_static() else _ensure_peer_cache(registry_url).get
            include_role = tool_desc_include_role()

            response = handle_mcp_request(
                body,
                self_name=self_name,
                registry_url=registry_url,
                timeout=60000,
                request_id=request_id,
                plugin_version=os.environ.get("CLAWCU_PLUGIN_VERSION") or "unknown",
                lookup_peer_fn=lookup_peer,
                forward_to_peer_fn=forward_to_peer,
                outbound_limiter=OUTBOUND_LIMITER,
                outbound_limit_key_fn=outbound_limit_key,
                list_peers_fn=list_peers,
                include_role=include_role,
            )
            return write_json_response(self, 200, response, rid_headers)

    return Handler


# ---- main ------------------------------------------------------------------

def main() -> None:
    args = parse_args(sys.argv[1:])
    local = bool(args.get("local"))

    if local:
        instance = args.get("instance") or args.get("name") or os.environ.get("A2A_SIDECAR_NAME") or socket.gethostname()
        adapter = make_local_adapter()
    else:
        instance = args.get("instance")
        if not instance:
            sys.stderr.write(
                "usage: python3 server.py --instance <name> --port <port> [--container <name>]\n"
                "   or: python3 server.py --local --port <port> [--name <name>]\n"
            )
            sys.exit(64)
        container = args.get("container") or f"clawcu-openclaw-{instance}"
        adapter = make_host_adapter(container)

    raw_port = args.get("port") or os.environ.get("A2A_SIDECAR_PORT")
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        sys.stderr.write("missing or invalid --port (or A2A_SIDECAR_PORT env)\n")
        sys.exit(64)
    if port <= 0:
        sys.stderr.write("missing or invalid --port (or A2A_SIDECAR_PORT env)\n")
        sys.exit(64)

    self_name = args.get("name") or os.environ.get("A2A_SIDECAR_NAME") or instance
    default_role = (
        f'OpenClaw agent "{self_name}"'
        if local
        else f'OpenClaw agent "{self_name}" (sidecar-bridged)'
    )
    role = args.get("role") or os.environ.get("A2A_SIDECAR_ROLE") or default_role
    raw_skills = args.get("skills") or os.environ.get("A2A_SIDECAR_SKILLS") or "chat,reason"
    skills = [s.strip() for s in str(raw_skills).split(",") if s.strip()]

    bind_host = "0.0.0.0" if local else "127.0.0.1"
    advertise_host = args.get("advertise-host") or os.environ.get("A2A_SIDECAR_ADVERTISE_HOST") or "127.0.0.1"
    advertise_port = _int_from_sources(
        args.get("advertise-port"),
        os.environ.get("A2A_SIDECAR_ADVERTISE_PORT"),
        default=port,
    )
    endpoint = f"http://{advertise_host}:{advertise_port}/a2a/send"

    gateway_host = (
        "127.0.0.1"
        if local
        else (args.get("gateway-host") or os.environ.get("A2A_GATEWAY_HOST") or "127.0.0.1")
    )
    gateway_port = _int_from_sources(
        args.get("gateway-port"),
        os.environ.get("A2A_GATEWAY_PORT"),
        os.environ.get("OPENCLAW_GATEWAY_PORT"),
        default=18789,
    )

    request_timeout_ms = _int_from_sources(
        args.get("request-timeout-ms"),
        os.environ.get("A2A_REQUEST_TIMEOUT_MS"),
        default=300000,
    )

    gateway_ready_deadline_ms = _int_from_sources(
        args.get("gateway-ready-deadline-ms"),
        os.environ.get("A2A_GATEWAY_READY_DEADLINE_MS"),
        default=30000,
    )

    gateway_ready_path_raw = (
        args.get("gateway-ready-path") or os.environ.get("A2A_GATEWAY_READY_PATH") or "/healthz"
    )
    gateway_ready_path = (
        gateway_ready_path_raw if gateway_ready_path_raw.startswith("/") else f"/{gateway_ready_path_raw}"
    )
    model = args.get("model") or os.environ.get("A2A_MODEL") or "openclaw"

    rate_limit_per_minute = _int_from_sources(
        args.get("rate-limit-per-minute"),
        os.environ.get("A2A_RATE_LIMIT_PER_MINUTE"),
        default=30,
    )
    rate_limiter = create_rate_limiter(per_minute=rate_limit_per_minute)

    thread_max_pairs = _int_from_sources(
        os.environ.get("A2A_THREAD_MAX_HISTORY_PAIRS"),
        default=10,
    )
    if thread_max_pairs < 0:
        thread_max_pairs = 10

    thread_store = create_thread_store(
        storage_dir=os.environ.get("A2A_THREAD_DIR") or "",
        max_history_pairs=thread_max_pairs,
    )

    card = {"name": self_name, "role": role, "skills": skills, "endpoint": endpoint}

    ctx = {
        "logger": default_logger,
        "self_name": self_name,
        "card": card,
        "adapter": adapter,
        "gateway_host": gateway_host,
        "gateway_port": gateway_port,
        "gateway_ready_path": gateway_ready_path,
        "gateway_ready_deadline_ms": gateway_ready_deadline_ms,
        "request_timeout_ms": request_timeout_ms,
        "model": model,
        "rate_limiter": rate_limiter,
        "thread_store": thread_store,
    }
    handler_cls = _make_handler_class(ctx)

    try:
        bootstrap_env = dict(os.environ)
        bootstrap_env["A2A_SIDECAR_PORT"] = str(port)
        bootstrap_env.setdefault("A2A_SERVICE_MCP_CONFIG_PATH", OPENCLAW_CONFIG_PATH)
        bootstrap_env.setdefault("A2A_SERVICE_MCP_CONFIG_FORMAT", "json")
        run_mcp_bootstrap(env=bootstrap_env, logger=default_logger)
    except Exception as err:
        default_logger.warn(
            f"[sidecar:{self_name}] mcp-bootstrap threw: {err}; continuing"
        )

    # Periodic empty-bucket sweep for OUTBOUND_LIMITER.
    create_sweep_timer(
        limiter=OUTBOUND_LIMITER,
        interval_ms=read_outbound_sweep_interval_ms(os.environ),
        logger=default_logger,
    )

    server = _ThreadingHTTPServer((bind_host, port), handler_cls)

    def _shutdown(sig, frame):  # noqa: ARG001
        default_logger.info(f"[sidecar:{self_name}] signal {sig}, closing...")
        threading.Thread(target=server.shutdown, daemon=True).start()
        # Hard backstop in case shutdown wedges.
        threading.Timer(2.0, lambda: os._exit(0)).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    default_logger.info(
        f"[sidecar:{self_name}] mode={adapter.mode} listening on http://{bind_host}:{port} "
        f"(endpoint={endpoint}, gateway={gateway_host}:{gateway_port})"
    )
    default_logger.info("  GET  /.well-known/agent-card.json")
    default_logger.info("  POST /a2a/send      → gateway /v1/chat/completions (native agent)")
    default_logger.info("  POST /a2a/outbound  → registry lookup → peer /a2a/send")
    default_logger.info("  POST /mcp           → MCP streamable-http (tool: a2a_call_peer)")

    try:
        server.serve_forever()
    finally:
        try:
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
