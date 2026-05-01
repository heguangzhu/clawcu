"""Tests for clawcu.a2a.client JSON-RPC send."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import pytest


def _start_jsonrpc_server(port, reply_text="Done"):
    """Start a fake A2A JSON-RPC server."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            method = body.get("method", "")
            if method == "message/send":
                result = {
                    "id": "task-123",
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"type": "text", "text": reply_text}]}],
                }
                resp = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            else:
                resp = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "not found"}}
            payload = json.dumps(resp).encode()
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


class TestJsonRpcClient:
    def test_post_message_jsonrpc(self):
        from clawcu.a2a.client import post_message_jsonrpc

        server = _start_jsonrpc_server(19898, reply_text="The answer is 42")
        try:
            result = post_message_jsonrpc(
                "http://127.0.0.1:19898",
                message="What is the answer?",
                timeout=5,
            )
            assert "id" in result
            assert result["status"]["state"] == "completed"
        finally:
            server.shutdown()

    def test_send_via_registry_jsonrpc(self):
        from clawcu.a2a.client import send_via_registry
        from clawcu.a2a.card import AgentCard
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        # Start a fake registry that returns a card pointing to our JSON-RPC server.
        jsonrpc_server = _start_jsonrpc_server(19897, reply_text="pong")

        class RegistryHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if "/agents/test-target" in self.path:
                    card = AgentCard(
                        name="test-target",
                        role="test",
                        endpoint="http://127.0.0.1:19897",
                    )
                    payload = json.dumps(card.to_dict()).encode()
                else:
                    payload = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        registry = ThreadingHTTPServer(("127.0.0.1", 19896), RegistryHandler)
        threading.Thread(target=registry.serve_forever, daemon=True).start()

        try:
            result = send_via_registry(
                registry_url="http://127.0.0.1:19896",
                sender="test-cli",
                target="test-target",
                message="ping",
                lookup_timeout=5,
                send_timeout=10,
            )
            assert "id" in result
        finally:
            jsonrpc_server.shutdown()
            registry.shutdown()
