from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from clawcu.a2a.card import AgentCard
from clawcu.a2a.sidecar_plugin._common.http_response import write_json_response as _write_json


def echo_reply(name: str, message: str) -> str:
    return f"[{name}] got: {message}"


def make_bridge_handler(
    card: AgentCard,
    *,
    reply_fn: Callable[[str, str], str] = echo_reply,
) -> type[BaseHTTPRequestHandler]:
    class BridgeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/.well-known/agent-card.json":
                _write_json(self, 200, card.to_dict())
                return
            _write_json(self, 404, {"error": "not_found", "path": self.path})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path != "/a2a/send":
                _write_json(self, 404, {"error": "not_found", "path": self.path})
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                _write_json(self, 400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                _write_json(self, 400, {"error": "invalid_body"})
                return
            message = payload.get("message", "")
            if not isinstance(message, str):
                _write_json(self, 400, {"error": "message_not_string"})
                return
            reply = reply_fn(card.name, message)
            _write_json(self, 200, {"from": card.name, "reply": reply})

    return BridgeHandler


def build_bridge_server(
    card: AgentCard,
    *,
    host: str = "127.0.0.1",
    port: int = 19100,
    reply_fn: Callable[[str, str], str] = echo_reply,
) -> ThreadingHTTPServer:
    handler = make_bridge_handler(card, reply_fn=reply_fn)
    return ThreadingHTTPServer((host, port), handler)


def serve_bridge_forever(
    card: AgentCard,
    *,
    host: str = "127.0.0.1",
    port: int = 19100,
    reply_fn: Callable[[str, str], str] = echo_reply,
    on_ready: Callable[[ThreadingHTTPServer], None] | None = None,
) -> None:
    server = build_bridge_server(card, host=host, port=port, reply_fn=reply_fn)
    if on_ready is not None:
        on_ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_bridge_in_thread(
    card: AgentCard,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    reply_fn: Callable[[str, str], str] = echo_reply,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = build_bridge_server(card, host=host, port=port, reply_fn=reply_fn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
