"""pytest port of tests/sidecar_mcp_e2e.test.js.

Spins up a stub registry + stub peer and the real Python sidecar, then
drives /mcp initialize, tools/list, tools/call (happy + unknown peer).
"""
from __future__ import annotations

import http.client
import json
import os
import re
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


def _start_stub(handler_fn):
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


def _wait_for_port(port, deadline_s=5.0):
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


def _start_sidecar(registry_url):
    port = _free_port()
    env = {
        **os.environ,
        "A2A_REGISTRY_URL": registry_url,
        "CLAWCU_PLUGIN_VERSION": "e2e-test",
        "A2A_GATEWAY_READY_DEADLINE_MS": "0",
        "A2A_SERVICE_MCP_CONFIG_PATH": "",
        "A2A_ENABLED": "false",
    }
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


# -- the test ---------------------------------------------------------------


def test_mcp_e2e_initialize_list_call_and_unknown_peer():
    # --- stub peer ---
    def peer_handle(h):
        if h.command != "POST" or h.path != "/a2a/send":
            h.send_response(404)
            h.send_header("content-length", "0")
            h.end_headers()
            return
        body = json.loads(_read_body(h))
        _write_json(h, 200, {"from": body["to"], "reply": f"pong:{body['message']}"})

    peer_srv, peer_url = _start_stub(peer_handle)

    # --- stub registry ---
    def reg_handle(h):
        m = re.match(r"^/agents/([^/?]+)", h.path or "")
        if not m:
            h.send_response(404)
            h.send_header("content-length", "0")
            h.end_headers()
            return
        name = m.group(1)
        if name == "ghost":
            _write_json(h, 404, {"error": "not_found"})
            return
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

    child, port, url = _start_sidecar(reg_url)
    try:
        mcp_url = f"http://127.0.0.1:{port}/mcp"

        # initialize
        status, _h, init_body, raw = _post_json(
            mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        assert status == 200, f"raw: {raw}"
        assert init_body["result"]["protocolVersion"] == "2024-11-05"
        assert init_body["result"]["serverInfo"]["name"] == "clawcu-a2a"
        assert init_body["result"]["serverInfo"]["version"] == "e2e-test"

        # tools/list
        status, _h, list_body, _raw = _post_json(
            mcp_url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert status == 200
        tools = list_body["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "a2a_call_peer"

        # tools/call happy
        status, headers, call_body, _raw = _post_json(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "a2a_call_peer",
                    "arguments": {"to": "analyst", "message": "hello"},
                },
            },
        )
        assert status == 200
        assert call_body["result"]["isError"] is False
        assert call_body["result"]["content"][0]["text"] == "pong:hello"
        assert any(k.lower() == "x-a2a-request-id" for k in headers)

        # tools/call unknown peer
        status, _h, ghost_body, _raw = _post_json(
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "a2a_call_peer",
                    "arguments": {"to": "ghost", "message": "hi"},
                },
            },
        )
        assert status == 200
        assert "error" in ghost_body
        assert ghost_body["error"]["code"] == -32001
        assert ghost_body["error"]["data"]["httpStatus"] == 404
    finally:
        _kill(child)
        _stop(peer_srv)
        _stop(reg_srv)
