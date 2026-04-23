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

import http.client
import http.server
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse, urlunparse

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
from _common.mcp import UpstreamError, handle_mcp_request  # noqa: E402
from _common.peer_cache import create_peer_cache as _shared_peer_cache  # noqa: E402
from _common.outbound_limit import (  # noqa: E402
    create_outbound_limiter,
    create_sweep_timer,
    key_for as outbound_limit_key,
    read_rpm as read_outbound_rpm,
    read_sweep_interval_ms as read_outbound_sweep_interval_ms,
)
from _common.protocol import (  # noqa: E402
    REQUEST_ID_HEADER,
    looks_like_request_id,
    read_hop_header,
    read_or_mint_request_id,
)
from _common.ratelimit import create_rate_limiter  # noqa: E402
from _common import streams as _streams  # noqa: E402
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

__all__ = [
    # Re-exported for tests and for any caller that used to import these
    # directly from server.py before the adapters/* split.
    "HostAdapter",
    "LocalAdapter",
    "OPENCLAW_AUTH_PATH",
    "OPENCLAW_CONFIG_PATH",
    "make_host_adapter",
    "make_local_adapter",
    "read_gateway_auth",
]

A2A_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
READ_JSON_BODY_LIMIT = 64 * 1024

# Hop budget read at module-scope so tests can import it without running main().
A2A_HOP_BUDGET = 8
try:
    _raw_hop = os.environ.get("A2A_HOP_BUDGET")
    if _raw_hop is not None and str(_raw_hop).strip() != "":
        _parsed_hop = int(_raw_hop)
        if _parsed_hop > 0:
            A2A_HOP_BUDGET = _parsed_hop
except (TypeError, ValueError):
    pass

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


# ---- HTTP helpers -----------------------------------------------------------

def parse_http_url(url: str) -> Dict[str, Any]:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise RuntimeError(f"invalid url '{url}': {exc}")
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"unsupported protocol in '{url}'")
    if not parsed.hostname:
        raise RuntimeError(f"invalid url '{url}': missing host")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    pathname = parsed.path or "/"
    search = f"?{parsed.query}" if parsed.query else ""
    return {
        "host": parsed.hostname,
        "port": int(port),
        "pathname": pathname,
        "search": search,
        "scheme": parsed.scheme,
    }


def _connection_for(host: str, port: int, timeout_s: float, scheme: str = "http"):
    if scheme == "https":
        return http.client.HTTPSConnection(host=host, port=port, timeout=timeout_s)
    return http.client.HTTPConnection(host=host, port=port, timeout=timeout_s)


# Reader + exception live in _common/streams.py so both sidecars share one
# implementation. ``_read_capped`` keeps its str-returning shape so existing
# callers (``raw = _read_capped(resp); json.loads(raw)``) need no change.
ResponseTooLarge = _streams.ResponseTooLarge


def _read_capped(resp, limit: int = A2A_MAX_RESPONSE_BYTES) -> str:
    return _streams.read_capped_text(resp, cap=limit)


def _http_call(
    *,
    method: str,
    host: str,
    port: int,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_ms: int,
    scheme: str = "http",
) -> Dict[str, Any]:
    """Shared connect/request/read/close skeleton for outbound HTTP.

    Both ``post_json`` and ``http_request_raw`` were 30-line near-identical
    copies of this shape (connect → request → capped-read → timeout/cap
    translation → close). The single difference — whether the caller sends
    a serialized body — is expressed here as an optional ``body`` argument.
    Exception translation (``ResponseTooLarge`` → ``RuntimeError``,
    ``socket.timeout`` → ``RuntimeError``) lives in one place so the two
    public wrappers stay thin.
    """
    conn = _connection_for(host, port, timeout_ms / 1000.0, scheme=scheme)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        status = resp.status or 0
        try:
            raw = _read_capped(resp)
        except ResponseTooLarge as exc:
            raise RuntimeError(str(exc))
        return {"status": status, "body": raw}
    except socket.timeout:
        raise RuntimeError(f"request timed out after {timeout_ms}ms")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def post_json(
    host: str,
    port: int,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body_obj=None,
    timeout_ms: int = 300000,
    scheme: str = "http",
) -> Dict[str, Any]:
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    merged = {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "user-agent": "a2a-bridge-sidecar/0.3",
    }
    if headers:
        merged.update(headers)
    return _http_call(
        method="POST",
        host=host,
        port=port,
        path=path,
        headers=merged,
        body=body,
        timeout_ms=timeout_ms,
        scheme=scheme,
    )


def http_request_raw(
    method: str,
    host: str,
    port: int,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_ms: int = 30000,
    scheme: str = "http",
) -> Dict[str, Any]:
    return _http_call(
        method=method,
        host=host,
        port=port,
        path=path,
        headers=headers,
        timeout_ms=timeout_ms,
        scheme=scheme,
    )


def fetch_peer_list(registry_url: str, timeout_ms: int) -> Optional[list]:
    parsed = parse_http_url(registry_url)
    base = parsed["pathname"].rstrip("/")
    path = f"{base}/agents"
    try:
        resp = http_request_raw(
            method="GET",
            host=parsed["host"],
            port=parsed["port"],
            path=path,
            headers={"accept": "application/json", "user-agent": "a2a-bridge-sidecar/0.3"},
            timeout_ms=timeout_ms,
            scheme=parsed["scheme"],
        )
    except Exception:
        return None
    if resp["status"] < 200 or resp["status"] >= 300:
        return None
    try:
        parsed_body = json.loads(resp["body"])
    except Exception:
        return None
    if not isinstance(parsed_body, list):
        return None
    return [p for p in parsed_body if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def create_peer_cache(
    registry_url: str,
    timeout_ms: int,
    fresh_ms: int = 30_000,
    stale_ms: int = 300_000,
    now_fn: Callable[[], int] = _default_now_ms,
    fetch_fn: Callable[..., Optional[list]] = fetch_peer_list,
):
    """TTL cache wrapping :func:`fetch_peer_list`. See
    :func:`_common.peer_cache.create_peer_cache` for the algorithm. This
    wrapper keeps OpenClaw's millisecond-unit external surface; internally
    everything runs in seconds against the shared implementation."""

    def _do_fetch():
        # Some callers pass a kwargs-style stub, the real fetch_peer_list
        # accepts both — try kwargs first so we don't break existing fakes.
        try:
            return fetch_fn(registry_url=registry_url, timeout_ms=timeout_ms)
        except TypeError:
            return fetch_fn(registry_url, timeout_ms)

    return _shared_peer_cache(
        _do_fetch,
        fresh_s=fresh_ms / 1000.0,
        stale_s=stale_ms / 1000.0,
        now_fn=lambda: now_fn() / 1000.0,
    )


def lookup_peer(registry_url: str, peer_name: str, timeout_ms: int) -> Dict[str, Any]:
    parsed = parse_http_url(registry_url)
    base = parsed["pathname"].rstrip("/")
    from urllib.parse import quote as _quote

    path = f"{base}/agents/{_quote(peer_name, safe='')}"
    resp = http_request_raw(
        method="GET",
        host=parsed["host"],
        port=parsed["port"],
        path=path,
        headers={"accept": "application/json", "user-agent": "a2a-bridge-sidecar/0.3"},
        timeout_ms=timeout_ms,
        scheme=parsed["scheme"],
    )
    status = resp["status"]
    body = resp["body"]
    if status == 404:
        raise UpstreamError(f"peer '{peer_name}' not found in registry", http_status=404)
    if status < 200 or status >= 300:
        raise UpstreamError(f"registry lookup {status}: {body[:200]}", http_status=503)
    try:
        card = json.loads(body)
    except Exception as exc:
        raise UpstreamError(f"registry returned non-json: {exc}", http_status=503)
    if not isinstance(card, dict) or not isinstance(card.get("endpoint"), str) or not card.get("endpoint"):
        raise UpstreamError(f"registry card for '{peer_name}' missing endpoint", http_status=503)
    return card


def forward_to_peer(
    endpoint: str,
    self_name: str,
    peer_name: str,
    message: str,
    thread_id: Optional[str],
    hop: int,
    timeout_ms: int,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    parsed = parse_http_url(endpoint)
    body_obj: Dict[str, Any] = {"from": self_name, "to": peer_name, "message": message}
    if thread_id:
        body_obj["thread_id"] = thread_id
    headers = {"x-a2a-hop": str(hop)}
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    try:
        resp = post_json(
            host=parsed["host"],
            port=parsed["port"],
            path=parsed["pathname"] + parsed["search"],
            headers=headers,
            body_obj=body_obj,
            timeout_ms=timeout_ms,
            scheme=parsed["scheme"],
        )
    except Exception as exc:
        raise UpstreamError(f"peer unreachable or timed out: {exc}", http_status=504)
    status = resp["status"]
    body = resp["body"]
    if 200 <= status < 300:
        try:
            return json.loads(body)
        except Exception as exc:
            raise UpstreamError(f"peer returned non-json: {exc}", http_status=502)
    if status == 508:
        raise UpstreamError(f"peer rejected hop limit: {body[:200]}", http_status=508)
    if status == 429:
        raise UpstreamError(f"peer rate-limited: {body[:200]}", http_status=429)
    raise UpstreamError(f"peer HTTP {status}: {body[:200]}", http_status=502, peer_status=status)


def read_allow_client_registry_url(env: Optional[Dict[str, str]] = None) -> bool:
    e = env if env is not None else os.environ
    raw = str(e.get("A2A_ALLOW_CLIENT_REGISTRY_URL") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def post_chat_completion(
    gateway_host: str,
    gateway_port: int,
    token: Optional[str],
    user_message: str,
    system_prompt: Optional[str],
    history: list,
    model: str,
    timeout_ms: int,
) -> str:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})
    payload = {
        "model": model or "openclaw",
        "stream": False,
        "messages": messages,
    }
    headers = {"authorization": f"Bearer {token}"} if token else {}
    resp = post_json(
        host=gateway_host,
        port=gateway_port,
        path="/v1/chat/completions",
        headers=headers,
        body_obj=payload,
        timeout_ms=timeout_ms,
    )
    status = resp["status"]
    body = resp["body"]
    if status != 200:
        raise RuntimeError(f"gateway /v1/chat/completions {status}: {body[:400]}")
    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"gateway returned non-json: {exc}")
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    content = None
    if isinstance(choice, dict):
        msg = choice.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
    if not isinstance(content, str) or not content:
        raise RuntimeError(f"gateway returned empty content: {body[:400]}")
    return content


def read_json_body(rfile, content_length: int, limit: int = READ_JSON_BODY_LIMIT):
    if content_length > limit:
        raise RuntimeError("request body too large")
    if content_length <= 0:
        return {}
    raw = b""
    remaining = content_length
    while remaining > 0:
        chunk = rfile.read(remaining)
        if not chunk:
            break
        raw += chunk
        if len(raw) > limit:
            raise RuntimeError("request body too large")
        remaining -= len(chunk)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid json: {exc}")


def build_a2a_context(self_name: str, from_agent: str) -> str:
    return (
        f'You are being addressed by a peer agent named "{from_agent}" '
        f'over the A2A bridge as "{self_name}". Respond in plain text, '
        f"preserving your own persona and skills. Keep the reply focused on "
        f"the peer's request; do not prefix with your own name."
    )


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

    def _read_request_json(handler) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            content_length = int(handler.headers.get("content-length") or "0")
        except (TypeError, ValueError):
            return None, "invalid content-length"
        try:
            return read_json_body(handler.rfile, content_length), None
        except RuntimeError as exc:
            return None, str(exc)

    def _ensure_peer_cache(registry_url: str):
        with peer_cache_holder["lock"]:
            if peer_cache_holder["cache"] is None:
                peer_cache_holder["cache"] = create_peer_cache(registry_url=registry_url, timeout_ms=5000)
            return peer_cache_holder["cache"]

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
            incoming_hop = read_hop_header(self.headers)
            request_id = read_or_mint_request_id(self.headers)
            rid_headers = {REQUEST_ID_HEADER: request_id}
            if incoming_hop >= A2A_HOP_BUDGET:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.send refused request_id={request_id} hop={incoming_hop} budget={A2A_HOP_BUDGET}"
                )
                return write_json_response(
                    self,
                    508,
                    {
                        "error": f"hop budget exceeded (hop={incoming_hop}, budget={A2A_HOP_BUDGET})",
                        "request_id": request_id,
                    },
                    rid_headers,
                )
            body, err = _read_request_json(self)
            if err is not None:
                return write_json_response(self, 400, {"error": err, "request_id": request_id}, rid_headers)
            if not isinstance(body.get("message"), str) or not body.get("message"):
                return write_json_response(self, 400, {"error": "missing 'message' (string)", "request_id": request_id}, rid_headers)
            if not isinstance(body.get("from"), str) or not body.get("from"):
                return write_json_response(self, 400, {"error": "missing 'from' (string)", "request_id": request_id}, rid_headers)

            thread_id = body.get("thread_id") if isinstance(body.get("thread_id"), str) and body.get("thread_id") else None
            if "thread_id" in body and thread_id is None:
                return write_json_response(
                    self,
                    400,
                    {
                        "error": "'thread_id' must be a non-empty string when provided",
                        "request_id": request_id,
                    },
                    rid_headers,
                )

            logger.info(
                f"[sidecar:{self_name}] a2a.send accepted request_id={request_id} from={body['from']} hop={incoming_hop}"
            )

            rl = rate_limiter.allow(body["from"])
            if not rl.ok:
                headers_out = dict(rid_headers)
                headers_out["Retry-After"] = str(max(1, int(rl.reset_ms / 1000 + 0.5)))
                return write_json_response(
                    self,
                    429,
                    {
                        "error": f"rate limit exceeded for peer '{body['from']}'",
                        "resetMs": rl.reset_ms,
                        "request_id": request_id,
                    },
                    headers_out,
                )

            try:
                auth = read_gateway_auth(adapter)
            except Exception as exc:
                return write_json_response(
                    self,
                    503,
                    {"error": f"instance not ready: {exc}", "request_id": request_id},
                    rid_headers,
                )

            ready = wait_for_gateway_ready(
                host=gateway_host,
                port=gateway_port,
                path=gateway_ready_path,
                deadline_ms=gateway_ready_deadline_ms,
            )
            if not ready:
                return write_json_response(
                    self,
                    503,
                    {
                        "error": f"gateway not ready after {gateway_ready_deadline_ms}ms",
                        "request_id": request_id,
                    },
                    rid_headers,
                )

            history = []
            if thread_id and thread_store.enabled:
                history = thread_store.load_history(body["from"], thread_id)

            try:
                reply = post_chat_completion(
                    gateway_host=gateway_host,
                    gateway_port=gateway_port,
                    token=auth.get("token"),
                    user_message=body["message"],
                    system_prompt=build_a2a_context(self_name, body["from"]),
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
                return write_json_response(
                    self,
                    502,
                    {"error": f"upstream agent failed: {exc}", "request_id": request_id},
                    rid_headers,
                )

            if thread_id and thread_store.enabled:
                thread_store.append_turn(body["from"], thread_id, body["message"], reply)

            logger.info(
                f"[sidecar:{self_name}] a2a.send replied request_id={request_id} from={body['from']}"
            )
            return write_json_response(
                self,
                200,
                {
                    "from": self_name,
                    "reply": reply,
                    "thread_id": thread_id,
                    "request_id": request_id,
                },
                rid_headers,
            )

        # ---- /a2a/outbound ---------------------------------------------------

        def _handle_a2a_outbound(self) -> None:
            incoming_hop = read_hop_header(self.headers)
            request_id = read_or_mint_request_id(self.headers)
            rid_headers = {REQUEST_ID_HEADER: request_id}
            if incoming_hop >= A2A_HOP_BUDGET:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound refused request_id={request_id} hop={incoming_hop} budget={A2A_HOP_BUDGET}"
                )
                return write_json_response(
                    self,
                    508,
                    {
                        "error": f"hop budget exceeded (hop={incoming_hop}, budget={A2A_HOP_BUDGET})",
                        "request_id": request_id,
                    },
                    rid_headers,
                )
            body, err = _read_request_json(self)
            if err is not None:
                return write_json_response(self, 400, {"error": err, "request_id": request_id}, rid_headers)
            if not isinstance(body.get("to"), str) or not body.get("to"):
                return write_json_response(self, 400, {"error": "missing 'to' (string)", "request_id": request_id}, rid_headers)
            if not isinstance(body.get("message"), str) or not body.get("message"):
                return write_json_response(self, 400, {"error": "missing 'message' (string)", "request_id": request_id}, rid_headers)

            out_thread_id = body.get("thread_id") if isinstance(body.get("thread_id"), str) and body.get("thread_id") else None
            if "thread_id" in body and out_thread_id is None:
                return write_json_response(
                    self,
                    400,
                    {
                        "error": "'thread_id' must be a non-empty string when provided",
                        "request_id": request_id,
                    },
                    rid_headers,
                )

            limit_key = outbound_limit_key(thread_id=out_thread_id, self_name=self_name)
            limit = OUTBOUND_LIMITER.check(limit_key)
            if not limit.allowed:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound self-rate-limited request_id={request_id} key={limit_key} limit={limit.limit}"
                )
                return write_json_response(
                    self,
                    429,
                    {
                        "error": f"self-origin rate limit exceeded ({limit.limit}/min)",
                        "request_id": request_id,
                        "retry_after_ms": limit.retry_after_ms,
                    },
                    rid_headers,
                )

            if "registry_url" in body:
                if not read_allow_client_registry_url(os.environ):
                    return write_json_response(
                        self,
                        400,
                        {
                            "error": "client-supplied 'registry_url' is disabled by server policy",
                            "request_id": request_id,
                        },
                        rid_headers,
                    )
                if not isinstance(body.get("registry_url"), str) or not body.get("registry_url"):
                    return write_json_response(
                        self,
                        400,
                        {
                            "error": "'registry_url' must be a non-empty string when provided",
                            "request_id": request_id,
                        },
                        rid_headers,
                    )
                registry_url = body["registry_url"]
            else:
                registry_url = os.environ.get("A2A_REGISTRY_URL") or "http://host.docker.internal:9100"

            try:
                timeout_ms_num = float(body.get("timeout_ms"))
            except (TypeError, ValueError):
                timeout_ms_num = float("nan")
            timeout_ms = int(timeout_ms_num) if timeout_ms_num == timeout_ms_num and timeout_ms_num > 0 else 60000

            logger.info(
                f"[sidecar:{self_name}] a2a.outbound begin request_id={request_id} to={body['to']} hop={incoming_hop}"
            )

            try:
                card_resp = lookup_peer(
                    registry_url=registry_url, peer_name=body["to"], timeout_ms=timeout_ms
                )
            except UpstreamError as exc:
                status = exc.http_status or 503
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound lookup-failed request_id={request_id} to={body['to']} status={status}"
                )
                return write_json_response(self, status, {"error": str(exc), "request_id": request_id}, rid_headers)
            except Exception as exc:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound lookup-failed request_id={request_id} to={body['to']} status=503"
                )
                return write_json_response(self, 503, {"error": str(exc), "request_id": request_id}, rid_headers)

            try:
                peer_resp = forward_to_peer(
                    endpoint=card_resp["endpoint"],
                    self_name=self_name,
                    peer_name=body["to"],
                    message=body["message"],
                    thread_id=out_thread_id,
                    hop=incoming_hop + 1,
                    timeout_ms=timeout_ms,
                    request_id=request_id,
                )
            except UpstreamError as exc:
                status = exc.http_status or 502
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound forward-failed request_id={request_id} to={body['to']} "
                    f"status={status} peer_status={exc.peer_status if exc.peer_status is not None else '-'}"
                )
                payload: Dict[str, Any] = {"error": str(exc), "request_id": request_id}
                if exc.peer_status is not None:
                    payload["peer_status"] = exc.peer_status
                return write_json_response(self, status, payload, rid_headers)
            except Exception as exc:
                logger.warn(
                    f"[sidecar:{self_name}] a2a.outbound forward-failed request_id={request_id} to={body['to']} status=502"
                )
                return write_json_response(self, 502, {"error": str(exc), "request_id": request_id}, rid_headers)

            logger.info(
                f"[sidecar:{self_name}] a2a.outbound done request_id={request_id} to={body['to']}"
            )
            return write_json_response(
                self,
                200,
                {
                    "from": self_name,
                    "to": body["to"],
                    "reply": peer_resp.get("reply") if isinstance(peer_resp.get("reply"), str) else "",
                    "thread_id": peer_resp.get("thread_id")
                    if isinstance(peer_resp.get("thread_id"), str)
                    else out_thread_id,
                    "request_id": request_id,
                },
                rid_headers,
            )

        # ---- /mcp ------------------------------------------------------------

        def _handle_mcp(self) -> None:
            request_id = read_or_mint_request_id(self.headers)
            rid_headers = {REQUEST_ID_HEADER: request_id}
            body, err = _read_request_json(self)
            if err is not None:
                return write_json_response(
                    self,
                    400,
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": err}},
                    rid_headers,
                )
            registry_url = os.environ.get("A2A_REGISTRY_URL") or "http://host.docker.internal:9100"
            method = body.get("method") if isinstance(body, dict) else None
            logger.info(f"[sidecar:{self_name}] mcp.request request_id={request_id} method={method}")
            cache = _ensure_peer_cache(registry_url)

            list_peers = None
            if os.environ.get("A2A_TOOL_DESC_MODE") != "static":
                list_peers = cache.get

            include_role = str(os.environ.get("A2A_TOOL_DESC_INCLUDE_ROLE") or "").lower() == "true"

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
    advertise_port_raw = args.get("advertise-port") or os.environ.get("A2A_SIDECAR_ADVERTISE_PORT") or port
    try:
        advertise_port = int(advertise_port_raw)
    except (TypeError, ValueError):
        advertise_port = port
    endpoint = f"http://{advertise_host}:{advertise_port}/a2a/send"

    gateway_host = (
        "127.0.0.1"
        if local
        else (args.get("gateway-host") or os.environ.get("A2A_GATEWAY_HOST") or "127.0.0.1")
    )
    gateway_port_raw = (
        args.get("gateway-port")
        or os.environ.get("A2A_GATEWAY_PORT")
        or os.environ.get("OPENCLAW_GATEWAY_PORT")
        or "18789"
    )
    try:
        gateway_port = int(gateway_port_raw)
    except (TypeError, ValueError):
        gateway_port = 18789

    try:
        request_timeout_ms = int(
            args.get("request-timeout-ms") or os.environ.get("A2A_REQUEST_TIMEOUT_MS") or "300000"
        )
    except (TypeError, ValueError):
        request_timeout_ms = 300000

    try:
        gateway_ready_deadline_ms = int(
            args.get("gateway-ready-deadline-ms")
            or os.environ.get("A2A_GATEWAY_READY_DEADLINE_MS")
            or "30000"
        )
    except (TypeError, ValueError):
        gateway_ready_deadline_ms = 30000

    gateway_ready_path_raw = (
        args.get("gateway-ready-path") or os.environ.get("A2A_GATEWAY_READY_PATH") or "/healthz"
    )
    gateway_ready_path = (
        gateway_ready_path_raw if gateway_ready_path_raw.startswith("/") else f"/{gateway_ready_path_raw}"
    )
    model = args.get("model") or os.environ.get("A2A_MODEL") or "openclaw"

    try:
        rate_limit_per_minute = int(
            args.get("rate-limit-per-minute")
            or os.environ.get("A2A_RATE_LIMIT_PER_MINUTE")
            or "30"
        )
    except (TypeError, ValueError):
        rate_limit_per_minute = 30
    rate_limiter = create_rate_limiter(per_minute=rate_limit_per_minute)

    try:
        thread_max_pairs = int(os.environ.get("A2A_THREAD_MAX_HISTORY_PAIRS") or "10")
        if thread_max_pairs < 0:
            thread_max_pairs = 10
    except (TypeError, ValueError):
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
