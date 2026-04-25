"""Hermes sidecar async-task plumbing: in-process handler tests.

Exercises the /a2a/send ``mode=async`` branch, /a2a/tasks/:id GET, and
/a2a/tasks/:id/cancel POST routes by instantiating the hermes handler in
the current process with a real ``TaskStore`` + ``TaskWorker`` and
stubbed gateway calls. No subprocess / docker; pure Python.

Mirrors the openclaw sidecar's ``test_sidecar_task_e2e.py`` at the
unit-integration level — we trust the openclaw e2e for full subprocess
coverage and rely on shared ``_common/task_*`` modules for the state
machine logic.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socketserver
import sys
import threading
import time
from http.server import HTTPServer

import pytest

_HERMES_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
        "hermes",
        "sidecar",
    )
)
_COMMON_PARENT = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
    )
)
# Both hermes and openclaw expose a module named ``server``. Loading one via
# plain ``import server`` pollutes ``sys.modules["server"]`` and breaks the
# other sidecar's tests when they run in the same pytest session. Load under
# a unique module name instead, then drop hermes-specific dirs off sys.path
# so later imports (openclaw sidecar, etc.) aren't shadowed.
_PATHS_ADDED: list[str] = []
for _p in (_HERMES_DIR, _COMMON_PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
        _PATHS_ADDED.append(_p)

_spec = importlib.util.spec_from_file_location(
    "hermes_sidecar_server", os.path.join(_HERMES_DIR, "server.py")
)
hermes_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hermes_server)

from _common.task_store import create_task_store  # noqa: E402
from _common.task_worker import TaskWorker  # noqa: E402

# Drop hermes/sidecar off sys.path so subsequent ``import server`` in other
# test files still resolves to openclaw. hermes-specific sibling modules
# (config/gateway/peering/inbound_limits) are already cached in sys.modules;
# wipe those names too — openclaw doesn't use them, and leaving them pinned
# could confuse future code that happens to reuse a colliding module name.
for _p in _PATHS_ADDED:
    try:
        sys.path.remove(_p)
    except ValueError:
        pass
for _name in ("server", "config", "gateway", "peering", "inbound_limits"):
    sys.modules.pop(_name, None)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _StubCfg:
    """Minimal ``Config``-shaped stub — only the fields build_handler touches."""

    bind_host = "127.0.0.1"
    bind_port = 0
    self_name = "javis"
    self_role = "hermes"
    self_skills = ["chat"]
    self_endpoint = "http://127.0.0.1/a2a/send"
    hermes_host = "127.0.0.1"
    hermes_port = 0
    api_key = "k"
    model = "hermes-agent"
    system_prompt = ""
    timeout = 5.0
    ready_deadline = 1.0
    ready_probe_timeout = 0.5
    ready_poll_interval = 0.1
    ready_path = "/health"
    thread_dir = ""
    thread_max_history_pairs = 10
    rate_limit_per_minute = 0
    inbound_request_timeout_s = 0
    allow_client_registry_url = False
    default_mode = "sync"

    def __init__(self, *, task_dir: str, default_mode: str = "sync"):
        self.task_dir = task_dir
        self.task_deadline_s = 600
        self.task_retain_s = 86400
        self.task_workers = 2
        self.task_heartbeat_s = 1.0
        self.default_mode = default_mode

    def agent_card(self):
        return {
            "name": self.self_name,
            "role": self.self_role,
            "skills": list(self.self_skills),
            "endpoint": self.self_endpoint,
        }


def _start_handler(cfg, task_store, task_worker):
    handler_cls = hermes_server.build_handler(
        cfg,
        task_store=task_store,
        task_worker=task_worker,
    )
    srv = _ThreadedHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _post_json(port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = json.dumps(body).encode("utf-8")
        merged = {"content-type": "application/json"}
        if headers:
            merged.update(headers)
        merged["content-length"] = str(len(payload))
        conn.request("POST", path, body=payload, headers=merged)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return resp.status, parsed
    finally:
        conn.close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return resp.status, parsed
    finally:
        conn.close()


def _install_worker(tmp_path, *, run_fn):
    store = create_task_store(storage_dir=str(tmp_path))
    worker = TaskWorker(
        store=store,
        run_fn=run_fn,
        logger=_Log(),
        self_name="javis",
        max_workers=1,
    )
    return store, worker


class _Log:
    def info(self, msg): pass
    def warn(self, msg): pass
    def error(self, msg): pass


@pytest.fixture
def _always_ready(monkeypatch):
    """Stub gateway readiness to always succeed so /a2a/send never 503s."""
    monkeypatch.setattr(hermes_server, "wait_for_gateway_ready", lambda cfg: True)


def test_async_send_returns_202_and_completes(tmp_path, _always_ready):
    def run_fn(snapshot):
        return {"reply": f"echo:{snapshot['input']['message']}", "thread_id": None}

    store, worker = _install_worker(tmp_path, run_fn=run_fn)
    cfg = _StubCfg(task_dir=str(tmp_path))
    srv, port = _start_handler(cfg, store, worker)
    try:
        status, body = _post_json(
            port, "/a2a/send",
            {"from": "alice", "message": "hi", "mode": "async"},
        )
        assert status == 202, body
        assert body["task_id"].startswith("task_")
        task_id = body["task_id"]
        deadline = time.monotonic() + 3.0
        snap = None
        while time.monotonic() < deadline:
            s, snap = _get(port, f"/a2a/tasks/{task_id}")
            assert s == 200
            if snap["state"] == "completed":
                break
            time.sleep(0.02)
        assert snap is not None and snap["state"] == "completed"
        assert snap["result"]["reply"] == "echo:hi"
    finally:
        worker.shutdown(wait=True)
        srv.shutdown()
        srv.server_close()


def test_async_cancel_flips_to_canceled(tmp_path, _always_ready):
    release = threading.Event()

    def run_fn(snapshot):
        release.wait(timeout=3.0)
        return {"reply": "late", "thread_id": None}

    store, worker = _install_worker(tmp_path, run_fn=run_fn)
    cfg = _StubCfg(task_dir=str(tmp_path))
    srv, port = _start_handler(cfg, store, worker)
    try:
        status, body = _post_json(
            port, "/a2a/send",
            {"from": "alice", "message": "hang", "mode": "async"},
        )
        assert status == 202
        task_id = body["task_id"]
        # Wait for working.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            _, snap = _get(port, f"/a2a/tasks/{task_id}")
            if snap["state"] == "working":
                break
            time.sleep(0.02)
        s, c = _post_json(port, f"/a2a/tasks/{task_id}/cancel", {})
        assert s == 200
        assert c["state"] == "canceled"
        release.set()
        time.sleep(0.2)
        _, snap = _get(port, f"/a2a/tasks/{task_id}")
        assert snap["state"] == "canceled"
    finally:
        release.set()
        worker.shutdown(wait=True)
        srv.shutdown()
        srv.server_close()


def test_default_mode_async_env_flips_omitted_mode(tmp_path, monkeypatch, _always_ready):
    monkeypatch.setenv("A2A_DEFAULT_MODE", "async")

    def run_fn(snapshot):
        return {"reply": "envdef", "thread_id": None}

    store, worker = _install_worker(tmp_path, run_fn=run_fn)
    # cfg.default_mode intentionally "sync" — env var should override.
    cfg = _StubCfg(task_dir=str(tmp_path), default_mode="sync")
    srv, port = _start_handler(cfg, store, worker)
    try:
        # No "mode" in body.
        status, body = _post_json(
            port, "/a2a/send",
            {"from": "alice", "message": "x"},
        )
        assert status == 202, body
        assert body["task_id"].startswith("task_")
    finally:
        worker.shutdown(wait=True)
        srv.shutdown()
        srv.server_close()


def test_sync_mode_without_task_store_is_unaffected(tmp_path, _always_ready, monkeypatch):
    """Omitting A2A_TASK_DIR should keep the sync path working — async
    branch must only activate on explicit ``mode=async`` and then returns
    503 if no task_store."""
    # Stub call_hermes so sync path completes without a gateway.
    monkeypatch.setattr(hermes_server, "call_hermes",
                        lambda cfg, msg, peer, history=None: f"sync:{msg}")
    cfg = _StubCfg(task_dir="")  # task dir disabled
    srv, port = _start_handler(cfg, None, None)
    try:
        status, body = _post_json(
            port, "/a2a/send", {"from": "alice", "message": "hey"},
        )
        assert status == 200
        assert body["reply"] == "sync:hey"
        # Explicit async without task_store → 503.
        status, body = _post_json(
            port, "/a2a/send",
            {"from": "alice", "message": "hey", "mode": "async"},
        )
        assert status == 503
    finally:
        srv.shutdown()
        srv.server_close()
