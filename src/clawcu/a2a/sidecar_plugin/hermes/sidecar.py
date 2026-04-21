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
- ``A2A_TIMEOUT_SECONDS`` (default ``120``)

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
        env A2A_SELF_NAME=javis python3 /opt/a2a/sidecar.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError


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


def _envs(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    if not v:
        return default
    return [s.strip() for s in v.split(",") if s.strip()]


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
        self.timeout = float(_envs("A2A_TIMEOUT_SECONDS", "120"))
        self.ready_deadline = float(_envs("A2A_GATEWAY_READY_DEADLINE_S", "30"))
        self.ready_probe_timeout = float(_envs("A2A_GATEWAY_READY_PROBE_S", "2"))
        self.ready_poll_interval = float(_envs("A2A_GATEWAY_READY_POLL_S", "0.5"))
        ready_path = _envs("A2A_GATEWAY_READY_PATH", "/health").strip() or "/health"
        self.ready_path = ready_path if ready_path.startswith("/") else f"/{ready_path}"
        # Review-14 P1-C: optional thread-history store. Disabled unless the
        # adapter sets A2A_THREAD_DIR (pointed at a datadir-mounted path).
        self.thread_dir = (os.environ.get("A2A_THREAD_DIR") or "").strip()
        try:
            raw_max = int(os.environ.get("A2A_THREAD_MAX_HISTORY_PAIRS") or "10")
        except ValueError:
            raw_max = 10
        self.thread_max_history_pairs = raw_max if raw_max >= 0 else 10

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


# --- Thread-history store (review-14 P1-C, mirror of openclaw thread.js) ---
#
# Design matches openclaw/sidecar/thread.js:
#   - append-only JSONL at <storageDir>/<peer>/<threadId>.jsonl
#   - SAFE_ID regex gates peer + thread_id, blocking path traversal
#   - load-time cap (maxHistoryPairs * 2 messages), file keeps everything
#   - disabled when storageDir is empty → no-op load/append
#
# Kept inline (not split into a module) because the hermes sidecar convention
# is single-file — see design-11 §4. ``thread`` is not a Python stdlib module
# name in 3.x, but ``ThreadStore`` + ``safe_id`` are scoped via module prefix
# to stay distinct from the ``threading`` stdlib domain.

_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def safe_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if not _SAFE_ID.match(value):
        return None
    if value in (".", ".."):
        return None
    return value


class ThreadStore:
    def __init__(self, storage_dir: str, max_history_pairs: int = 10) -> None:
        self.storage_dir = storage_dir or ""
        self.enabled = bool(self.storage_dir)
        self.max_history_pairs = max_history_pairs if max_history_pairs >= 0 else 10

    def _thread_file(self, peer: str, thread_id: str) -> tuple[str, str] | None:
        p = safe_id(peer)
        t = safe_id(thread_id)
        if not p or not t:
            return None
        dir_ = os.path.join(self.storage_dir, p)
        return dir_, os.path.join(dir_, f"{t}.jsonl")

    def load_history(self, peer: str, thread_id: str) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        paths = self._thread_file(peer, thread_id)
        if paths is None:
            return []
        _dir, file_path = paths
        try:
            with open(file_path, encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            sys.stderr.write(
                f"a2a-sidecar: thread load failed for {peer}/{thread_id}: {exc}\n"
            )
            return []
        out: list[dict[str, str]] = []
        for line in raw.split("\n"):
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                parsed = json.loads(trimmed)
            except Exception:
                # Corrupt line — skip but keep loading; a partial write
                # shouldn't poison the whole thread's replay.
                continue
            if not isinstance(parsed, dict):
                continue
            role = parsed.get("role")
            content = parsed.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            out.append({"role": role, "content": content})
        cap = max(0, self.max_history_pairs) * 2
        if cap > 0 and len(out) > cap:
            return out[-cap:]
        return out

    def append_turn(
        self, peer: str, thread_id: str, user_msg: str, assistant_msg: str
    ) -> bool:
        if not self.enabled:
            return False
        paths = self._thread_file(peer, thread_id)
        if paths is None:
            return False
        if not isinstance(user_msg, str) or not isinstance(assistant_msg, str):
            return False
        dir_, file_path = paths
        try:
            os.makedirs(dir_, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            lines = (
                json.dumps({"role": "user", "content": user_msg, "ts": ts})
                + "\n"
                + json.dumps({"role": "assistant", "content": assistant_msg, "ts": ts})
                + "\n"
            )
            with open(file_path, "a", encoding="utf-8") as fh:
                fh.write(lines)
            return True
        except OSError as exc:
            sys.stderr.write(
                f"a2a-sidecar: thread append failed for {peer}/{thread_id}: {exc}\n"
            )
            return False


# Cache a recent "ready" observation so we don't probe on every /a2a/send.
# 5-minute TTL matches the openclaw sidecar.
_GATEWAY_READY_UNTIL = 0.0


def _probe_gateway_ready(cfg: Config) -> bool:
    req = urllib.request.Request(cfg.health_url(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=cfg.ready_probe_timeout) as resp:
            return 200 <= resp.status < 400
    except (HTTPError, URLError, OSError, TimeoutError):
        return False


def wait_for_gateway_ready(cfg: Config, now_fn=None, sleep_fn=None) -> bool:
    """Block until Hermes /health responds 2xx or the deadline elapses.

    Returns True if the gateway became ready, False on timeout. Called lazily
    from /a2a/send so an early-arriving peer request doesn't 502 just because
    the supervisor hasn't finished bringing Hermes up yet.
    """
    import time as _time

    global _GATEWAY_READY_UNTIL
    now = now_fn or _time.time
    sleep = sleep_fn or _time.sleep
    if now() < _GATEWAY_READY_UNTIL:
        return True
    deadline = now() + cfg.ready_deadline
    while now() < deadline:
        if _probe_gateway_ready(cfg):
            _GATEWAY_READY_UNTIL = now() + 5 * 60
            return True
        sleep(cfg.ready_poll_interval)
    return False


def invalidate_gateway_ready_cache() -> None:
    """Drop the "gateway is ready" cache so the next ``/a2a/send`` re-probes.

    Called after upstream signals that suggest the gateway may have died
    mid-flight (unreachable socket, 5xx): the 5-minute TTL otherwise lets the
    sidecar keep pushing into a dead gateway without re-probing, turning one
    gateway flake into a 5-minute outage.
    """
    global _GATEWAY_READY_UNTIL
    _GATEWAY_READY_UNTIL = 0.0


def call_hermes(
    cfg: Config,
    message: str,
    peer_from: str,
    history: list[dict[str, str]] | None = None,
) -> str:
    """POST to Hermes' OpenAI-compat /v1/chat/completions, return assistant text."""

    messages: list[dict[str, str]] = []
    if cfg.system_prompt:
        messages.append({"role": "system", "content": cfg.system_prompt})
    if history:
        messages.extend(history)
    # Tag the incoming message with its A2A origin so the LLM has context.
    prefix = f"[from agent '{peer_from}'] " if peer_from else ""
    messages.append({"role": "user", "content": prefix + message})

    body = json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "stream": False,
        }
    ).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    req = urllib.request.Request(
        cfg.chat_url(), data=body, method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"unexpected chat response shape: {payload!r}"
        ) from exc


def _write_json(h: BaseHTTPRequestHandler, status: int, obj: Any) -> None:
    data = json.dumps(obj).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def build_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    thread_store = ThreadStore(
        storage_dir=cfg.thread_dir,
        max_history_pairs=cfg.thread_max_history_pairs,
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-bridge-sidecar/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/.well-known/agent-card.json":
                _write_json(self, 200, cfg.agent_card())
                return
            if self.path in ("/health", "/healthz"):
                # Accept both spellings so external callers don't need to know
                # which service the sidecar wraps (review-7 P2-E): hermes uses
                # /health internally, openclaw uses /healthz, and the sidecar
                # layer hides that by responding to both.
                _write_json(
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
            _write_json(self, 404, {"error": f"not found: {self.path}"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/a2a/send":
                _write_json(self, 404, {"error": f"not found: {self.path}"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                _write_json(self, 400, {"error": f"bad json: {exc}"})
                return

            if not isinstance(payload, dict):
                _write_json(self, 400, {"error": "body must be a JSON object"})
                return

            peer_from = str(payload.get("from") or "")
            peer_to = str(payload.get("to") or "")
            message = payload.get("message")
            if not isinstance(message, str) or not message:
                _write_json(self, 400, {"error": "`message` must be a non-empty string"})
                return

            # Review-14 P1-C: thread_id is OPTIONAL. Absent → stateless turn
            # (identical to pre-iter-14 behavior). Present but wrong type
            # (empty / non-string) → 400 so the peer knows context won't
            # land, rather than silently dropping it.
            raw_thread_id = payload.get("thread_id", None)
            if raw_thread_id is None:
                thread_id: str | None = None
            elif isinstance(raw_thread_id, str) and raw_thread_id:
                thread_id = raw_thread_id
            else:
                _write_json(
                    self,
                    400,
                    {"error": "'thread_id' must be a non-empty string when provided"},
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
                _write_json(
                    self,
                    503,
                    {"error": f"gateway not ready after {cfg.ready_deadline}s"},
                )
                return

            history: list[dict[str, str]] = []
            if thread_id and thread_store.enabled:
                history = thread_store.load_history(peer_from, thread_id)

            try:
                reply = call_hermes(cfg, message, peer_from, history=history)
            except HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                log.error("Hermes HTTPError %s: %s", e.code, body[:500])
                if e.code >= 500:
                    # 5xx suggests gateway is sick; drop the ready-cache so the
                    # next request re-probes instead of blindly retrying.
                    invalidate_gateway_ready_cache()
                _write_json(
                    self,
                    502,
                    {"error": f"upstream Hermes HTTP {e.code}", "detail": body[:500]},
                )
                return
            except URLError as e:
                log.error("Hermes URLError: %s", e)
                # Socket failures mean the gateway process is almost certainly
                # gone; invalidate so the sidecar doesn't keep pushing blind
                # for the remainder of the 5-min TTL.
                invalidate_gateway_ready_cache()
                _write_json(
                    self, 502, {"error": f"upstream Hermes unreachable: {e.reason}"}
                )
                return
            except Exception as e:  # noqa: BLE001 — we want to surface anything
                log.exception("unexpected error while calling Hermes")
                _write_json(self, 500, {"error": f"internal: {e}"})
                return

            if thread_id and thread_store.enabled:
                thread_store.append_turn(peer_from, thread_id, message, reply)

            _write_json(
                self,
                200,
                {"from": cfg.self_name, "reply": reply, "thread_id": thread_id},
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

    server = ThreadingHTTPServer((cfg.bind_host, cfg.bind_port), build_handler(cfg))
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
