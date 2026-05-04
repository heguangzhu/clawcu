"""Tests for clawcu.a2a.adapter.executor — gateway call logic."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import pytest

a2a_sdk = pytest.importorskip("a2a", reason="a2a-sdk not installed")


def _start_gateway(port, reply_text="Hello from gateway"):
    """Start a fake gateway that handles /v1/chat/completions and /healthz."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            reply = {
                "choices": [{"message": {"content": reply_text}}],
            }
            payload = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


@pytest.fixture(autouse=True)
def _isolate_gateway(monkeypatch):
    """Reset module-level gateway config for each test."""
    import clawcu.a2a.adapter.executor as exc_mod
    monkeypatch.setattr(exc_mod, "_GATEWAY_TIMEOUT", 10.0)


class TestCallGateway:
    def test_call_gateway_success(self, monkeypatch):
        from clawcu.a2a.adapter.executor import _call_gateway
        import clawcu.a2a.adapter.executor as exc_mod

        monkeypatch.setattr(exc_mod, "_GATEWAY_URL", "http://127.0.0.1:19899")
        server = _start_gateway(19899, reply_text="42")
        try:
            result = asyncio.run(_call_gateway("What is 6x7?", ""))
            assert result == "42"
        finally:
            server.shutdown()

    def test_call_gateway_empty_reply(self, monkeypatch):
        from clawcu.a2a.adapter.executor import _call_gateway
        import clawcu.a2a.adapter.executor as exc_mod

        monkeypatch.setattr(exc_mod, "_GATEWAY_URL", "http://127.0.0.1:19898")
        server = _start_gateway(19898, reply_text="")
        try:
            result = asyncio.run(_call_gateway("hello", ""))
            assert result == ""
        finally:
            server.shutdown()

    def test_check_gateway_ready(self, monkeypatch):
        from clawcu.a2a.adapter.executor import _check_gateway_ready
        import clawcu.a2a.adapter.executor as exc_mod

        monkeypatch.setattr(exc_mod, "_GATEWAY_URL", "http://127.0.0.1:19897")
        monkeypatch.setattr(exc_mod, "_GATEWAY_READY_PATH", "/")
        server = _start_gateway(19897)
        try:
            assert asyncio.run(_check_gateway_ready()) is True
        finally:
            server.shutdown()

    def test_check_gateway_not_ready(self, monkeypatch):
        from clawcu.a2a.adapter.executor import _check_gateway_ready
        import clawcu.a2a.adapter.executor as exc_mod

        monkeypatch.setattr(exc_mod, "_GATEWAY_URL", "http://127.0.0.1:19999")
        result = asyncio.run(_check_gateway_ready())
        assert result is False
