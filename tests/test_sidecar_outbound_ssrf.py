"""pytest port of tests/sidecar_outbound_ssrf.test.js.

Scope: SSRF guard on /a2a/outbound's body-supplied `registry_url`.
Spawns the Python sidecar as a subprocess and exercises the running
HTTP surface.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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

import server as sidecar  # noqa: E402


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


def _start_stub(handler_fn, method="ANY"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):  # noqa: N802
            handler_fn(self)

        def do_POST(self):  # noqa: N802
            handler_fn(self)

    srv = _ThreadedHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def _read_body(handler):
    length = int(handler.headers.get("content-length") or "0")
    raw = handler.rfile.read(length) if length > 0 else b""
    return raw.decode("utf-8")


def _write_json(handler, status, obj):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _post_json(url, body, headers=None):
    """POST JSON, return (status, headers, parsed_body_or_none, raw)."""
    from urllib.parse import urlparse

    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    try:
        payload = json.dumps(body).encode("utf-8")
        merged = {"content-type": "application/json"}
        if headers:
            merged.update(headers)
        merged["content-length"] = str(len(payload))
        conn.request("POST", u.path, body=payload, headers=merged)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return resp.status, dict(resp.getheaders()), parsed, raw
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


def _start_sidecar(extra_env=None):
    port = _free_port()
    env = {
        **os.environ,
        "A2A_REGISTRY_URL": "http://127.0.0.1:0",
        "CLAWCU_PLUGIN_VERSION": "e2e-test",
        "A2A_GATEWAY_READY_DEADLINE_MS": "0",
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
            "writer",
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
    return child, port, f"http://127.0.0.1:{port}"


def _kill(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


# -- tests ------------------------------------------------------------------


def test_body_registry_url_rejected_when_flag_unset():
    child, port, url = _start_sidecar()
    try:
        status, _hdrs, body, _raw = _post_json(
            f"{url}/a2a/outbound",
            {
                "to": "attacker",
                "message": "probe",
                "registry_url": "http://attacker.example/registry",
            },
        )
        assert status == 400
        assert body is not None
        assert "disabled by server policy" in body["error"]
        assert isinstance(body.get("request_id"), str) and body["request_id"]
    finally:
        _kill(child)


def test_body_registry_url_allowed_when_env_flag_set():
    def peer_handle(h):
        if h.command != "POST" or h.path != "/a2a/send":
            h.send_response(404)
            h.send_header("content-length", "0")
            h.end_headers()
            return
        body = json.loads(_read_body(h))
        _write_json(h, 200, {"from": body["to"], "reply": f"pong:{body['message']}"})

    peer_srv, peer_url = _start_stub(peer_handle)

    lookups = {"n": 0}

    def reg_handle(h):
        import re

        m = re.match(r"^/agents/([^/?]+)", h.path or "")
        if not m:
            h.send_response(404)
            h.send_header("content-length", "0")
            h.end_headers()
            return
        lookups["n"] += 1
        name = m.group(1)
        if name != "analyst":
            h.send_response(404)
            h.send_header("content-length", "0")
            h.end_headers()
            return
        _write_json(
            h,
            200,
            {
                "name": "analyst",
                "role": "analyst",
                "skills": ["data"],
                "endpoint": f"{peer_url}/a2a/send",
            },
        )

    reg_srv, reg_url = _start_stub(reg_handle)

    child, port, url = _start_sidecar({"A2A_ALLOW_CLIENT_REGISTRY_URL": "1"})
    try:
        status, _hdrs, body, raw = _post_json(
            f"{url}/a2a/outbound",
            {"to": "analyst", "message": "ping", "registry_url": reg_url},
        )
        assert status == 200, f"body: {raw}"
        assert body["reply"] == "pong:ping"
        assert lookups["n"] == 1
    finally:
        _kill(child)
        _stop(peer_srv)
        _stop(reg_srv)


def test_body_registry_url_non_string_still_rejected_with_flag_on():
    child, port, url = _start_sidecar({"A2A_ALLOW_CLIENT_REGISTRY_URL": "1"})
    try:
        status, _hdrs, body, _raw = _post_json(
            f"{url}/a2a/outbound",
            {"to": "analyst", "message": "ping", "registry_url": None},
        )
        assert status == 400
        assert "non-empty string" in body["error"]
    finally:
        _kill(child)


# -- unit-level parse test --------------------------------------------------


def test_read_allow_client_registry_url_parses_env_var():
    f = sidecar.read_allow_client_registry_url
    assert f({}) is False
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": ""}) is False
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": "0"}) is False
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": "false"}) is False
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": "1"}) is True
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": "true"}) is True
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": "YES"}) is True
    assert f({"A2A_ALLOW_CLIENT_REGISTRY_URL": " on "}) is True
