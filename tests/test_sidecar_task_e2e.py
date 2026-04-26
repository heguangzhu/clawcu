"""End-to-end HTTP test: /a2a/send?mode=async → 202, GET /tasks/:id → completed.

Boots the real Python sidecar as a subprocess against a stub gateway +
stub registry, then walks the async task lifecycle over the wire.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver

import pytest

_SIDECAR_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
        "openclaw",
        "sidecar",
    )
)
if _SIDECAR_DIR not in sys.path:
    sys.path.insert(0, _SIDECAR_DIR)

SERVER_PATH = os.path.join(_SIDECAR_DIR, "server.py")


# -- helpers ----------------------------------------------------------------


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_stub(handler_fn, port=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):  # noqa: N802
            handler_fn(self)

        def do_POST(self):  # noqa: N802
            handler_fn(self)

    bind = ("127.0.0.1", port or 0)
    srv = _ThreadedHTTPServer(bind, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def _read_body(h):
    length = int(h.headers.get("content-length") or "0")
    return h.rfile.read(length) if length > 0 else b""


def _write_json(h, status, obj):
    body = json.dumps(obj).encode("utf-8")
    h.send_response(status)
    h.send_header("content-type", "application/json")
    h.send_header("content-length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def _write_sse(h, content):
    """Stream ``content`` as a single SSE delta + ``[DONE]``.

    The async/task path on the openclaw sidecar drives the gateway with
    ``stream: true`` and reads ``data: …`` lines. Stub gateways for that
    path must therefore speak SSE — a plain JSON body produces an empty
    chunk list and the worker fails the task with ``gateway streamed
    empty content``.
    """
    delta = json.dumps({"choices": [{"delta": {"content": content}}]})
    payload = (
        f"data: {delta}\n\n"
        "data: [DONE]\n\n"
    ).encode("utf-8")
    h.send_response(200)
    h.send_header("content-type", "text/event-stream")
    h.send_header("content-length", str(len(payload)))
    h.end_headers()
    h.wfile.write(payload)


def _post_json(host, port, path, body, headers=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
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
        return resp.status, parsed, raw
    finally:
        conn.close()


def _get(host, port, path):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return resp.status, parsed, raw
    finally:
        conn.close()


def _wait_for_port(port, deadline_s=30.0):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            conn.request("GET", "/.well-known/agent-card.json")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"sidecar didn't bind :{port} within {deadline_s}s")


def _start_sidecar(*, gateway_port, task_dir, extra_env=None):
    port = _free_port()
    # Write a fake openclaw.json so read_gateway_auth succeeds.
    config_path = os.path.join(task_dir, "openclaw.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump({"gateway": {"auth": {"mode": "token", "token": "test-token"}}}, fh)
    env = {
        **os.environ,
        "A2A_GATEWAY_HOST": "127.0.0.1",
        "A2A_GATEWAY_PORT": str(gateway_port),
        "A2A_GATEWAY_READY_DEADLINE_MS": "1000",
        "A2A_GATEWAY_READY_PATH": "/healthz",
        "A2A_TASK_DIR": task_dir,
        "A2A_TASK_HEARTBEAT_S": "1.0",
        "OPENCLAW_CONFIG_PATH": config_path,
        "OPENCLAW_AUTH_PATH": config_path,
        "CLAWCU_PLUGIN_VERSION": "e2e-test",
        "A2A_SERVICE_MCP_CONFIG_PATH": "",
        "A2A_ENABLED": "false",
    }
    if extra_env:
        env.update(extra_env)
    child = subprocess.Popen(
        [
            sys.executable,
            SERVER_PATH,
            "--local",
            "--port",
            str(port),
            "--name",
            "bob",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_port(port)
    except Exception:
        child.terminate()
        try:
            out, err = child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
        raise RuntimeError(
            f"sidecar boot failed\nstdout: {out.decode()}\nstderr: {err.decode()}"
        )
    return child, port


def _kill(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


# -- tests ------------------------------------------------------------------


def test_async_send_returns_202_and_completes(tmp_path):
    # Stub gateway that replies to /v1/chat/completions + /healthz.
    def gateway_handle(h):
        if h.path == "/healthz":
            _write_json(h, 200, {"ok": True})
            return
        if h.command == "POST" and h.path == "/v1/chat/completions":
            body = json.loads(_read_body(h))
            user_msg = ""
            for m in body.get("messages", []):
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
            reply = f"echo: {user_msg}"
            if body.get("stream"):
                _write_sse(h, reply)
            else:
                _write_json(h, 200, {
                    "choices": [
                        {"message": {"role": "assistant", "content": reply}}
                    ],
                })
            return
        h.send_response(404)
        h.send_header("content-length", "0")
        h.end_headers()

    gw_srv, gw_port = _start_stub(gateway_handle)
    try:
        child, port = _start_sidecar(gateway_port=gw_port, task_dir=str(tmp_path))
        try:
            status, body, _ = _post_json(
                "127.0.0.1", port, "/a2a/send",
                {"from": "alice", "message": "hello", "mode": "async"},
            )
            assert status == 202
            assert isinstance(body.get("task_id"), str)
            assert body["task_id"].startswith("task_")
            assert body["state"] == "submitted"
            task_id = body["task_id"]

            # Poll until completed.
            deadline = time.monotonic() + 5.0
            snap = None
            while time.monotonic() < deadline:
                s, snap, _ = _get("127.0.0.1", port, f"/a2a/tasks/{task_id}")
                assert s == 200
                if snap["state"] == "completed":
                    break
                time.sleep(0.05)
            assert snap is not None
            assert snap["state"] == "completed"
            assert snap["result"]["reply"] == "echo: hello"
        finally:
            _kill(child)
    finally:
        _stop(gw_srv)


def test_async_cancel_flips_to_canceled(tmp_path):
    # Gateway that hangs on chat completion until released.
    release = threading.Event()

    def gateway_handle(h):
        if h.path == "/healthz":
            _write_json(h, 200, {"ok": True})
            return
        if h.command == "POST" and h.path == "/v1/chat/completions":
            _read_body(h)
            release.wait(timeout=5.0)
            _write_json(h, 200, {
                "choices": [{"message": {"role": "assistant", "content": "late"}}],
            })
            return
        h.send_response(404)
        h.send_header("content-length", "0")
        h.end_headers()

    gw_srv, gw_port = _start_stub(gateway_handle)
    try:
        child, port = _start_sidecar(gateway_port=gw_port, task_dir=str(tmp_path))
        try:
            status, body, _ = _post_json(
                "127.0.0.1", port, "/a2a/send",
                {"from": "alice", "message": "hang", "mode": "async"},
            )
            assert status == 202
            task_id = body["task_id"]

            # Wait for state=working before canceling.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                _, snap, _ = _get("127.0.0.1", port, f"/a2a/tasks/{task_id}")
                if snap["state"] == "working":
                    break
                time.sleep(0.05)

            # Cancel.
            s, c, _ = _post_json("127.0.0.1", port, f"/a2a/tasks/{task_id}/cancel", {})
            assert s == 200
            assert c["state"] == "canceled"

            # Let gateway finish; worker should not overwrite terminal.
            release.set()
            time.sleep(0.3)
            _, snap, _ = _get("127.0.0.1", port, f"/a2a/tasks/{task_id}")
            assert snap["state"] == "canceled"
        finally:
            _kill(child)
    finally:
        _stop(gw_srv)


def test_default_mode_async_flips_omitted_mode_to_async(tmp_path):
    """With ``A2A_DEFAULT_MODE=async`` a request without ``mode`` should
    land on the async branch (202 + task_id) instead of the sync 200 path."""
    def gateway_handle(h):
        if h.path == "/healthz":
            _write_json(h, 200, {"ok": True})
            return
        if h.command == "POST" and h.path == "/v1/chat/completions":
            body = json.loads(_read_body(h))
            user_msg = next(
                (m["content"] for m in body.get("messages", []) if m.get("role") == "user"),
                "",
            )
            reply = f"async-default:{user_msg}"
            if body.get("stream"):
                _write_sse(h, reply)
            else:
                _write_json(h, 200, {
                    "choices": [{"message": {"role": "assistant", "content": reply}}],
                })
            return
        h.send_response(404)
        h.send_header("content-length", "0")
        h.end_headers()

    gw_srv, gw_port = _start_stub(gateway_handle)
    try:
        child, port = _start_sidecar(
            gateway_port=gw_port,
            task_dir=str(tmp_path),
            extra_env={"A2A_DEFAULT_MODE": "async"},
        )
        try:
            # No ``mode`` in payload — env default should promote it to async.
            status, body, _ = _post_json(
                "127.0.0.1", port, "/a2a/send",
                {"from": "alice", "message": "envdef"},
            )
            assert status == 202, f"expected 202 with A2A_DEFAULT_MODE=async, got {status}: {body}"
            task_id = body["task_id"]
            deadline = time.monotonic() + 5.0
            snap = None
            while time.monotonic() < deadline:
                s, snap, _ = _get("127.0.0.1", port, f"/a2a/tasks/{task_id}")
                assert s == 200
                if snap["state"] == "completed":
                    break
                time.sleep(0.05)
            assert snap is not None and snap["state"] == "completed"
            assert snap["result"]["reply"] == "async-default:envdef"

            # Explicit mode=sync should still override the env default.
            status, body, _ = _post_json(
                "127.0.0.1", port, "/a2a/send",
                {"from": "alice", "message": "forced-sync", "mode": "sync"},
            )
            assert status == 200
            assert body["reply"] == "async-default:forced-sync"
            assert "task_id" not in body
        finally:
            _kill(child)
    finally:
        _stop(gw_srv)


def test_sync_mode_still_works(tmp_path):
    """Regression: a request without ``mode`` or ``mode:sync`` keeps the
    v0 sync response shape."""
    def gateway_handle(h):
        if h.path == "/healthz":
            _write_json(h, 200, {"ok": True})
            return
        if h.command == "POST" and h.path == "/v1/chat/completions":
            body = json.loads(_read_body(h))
            user_msg = next(
                (m["content"] for m in body.get("messages", []) if m.get("role") == "user"),
                "",
            )
            _write_json(h, 200, {
                "choices": [{"message": {"role": "assistant", "content": f"sync:{user_msg}"}}],
            })
            return
        h.send_response(404)
        h.send_header("content-length", "0")
        h.end_headers()

    gw_srv, gw_port = _start_stub(gateway_handle)
    try:
        child, port = _start_sidecar(gateway_port=gw_port, task_dir=str(tmp_path))
        try:
            status, body, _ = _post_json(
                "127.0.0.1", port, "/a2a/send",
                {"from": "alice", "message": "hey"},
            )
            assert status == 200
            assert body["from"] == "bob"
            assert body["reply"] == "sync:hey"
            assert "task_id" not in body
        finally:
            _kill(child)
    finally:
        _stop(gw_srv)
