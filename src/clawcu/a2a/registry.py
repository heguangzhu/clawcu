from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable

from clawcu.a2a.card import AgentCard, card_from_record

CardProvider = Callable[[], Iterable[AgentCard]]


def cards_from_service(service, *, host: str = "127.0.0.1") -> list[AgentCard]:
    records = service.list_instances(running_only=True)
    return [card_from_record(rec, host=host) for rec in records]


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_registry_handler(provider: CardProvider) -> type[BaseHTTPRequestHandler]:
    class RegistryHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/agents":
                cards = [c.to_dict() for c in provider()]
                _write_json(self, 200, cards)
                return
            if path.startswith("/agents/"):
                name = path[len("/agents/"):]
                for card in provider():
                    if card.name == name:
                        _write_json(self, 200, card.to_dict())
                        return
                _write_json(self, 404, {"error": "not_found", "name": name})
                return
            _write_json(self, 404, {"error": "not_found", "path": self.path})

    return RegistryHandler


def build_registry_server(
    provider: CardProvider,
    *,
    host: str = "127.0.0.1",
    port: int = 9100,
) -> ThreadingHTTPServer:
    handler = make_registry_handler(provider)
    return ThreadingHTTPServer((host, port), handler)


def serve_registry_forever(
    provider: CardProvider,
    *,
    host: str = "127.0.0.1",
    port: int = 9100,
    on_ready: Callable[[ThreadingHTTPServer], None] | None = None,
) -> None:
    server = build_registry_server(provider, host=host, port=port)
    if on_ready is not None:
        on_ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_registry_in_thread(
    provider: CardProvider,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = build_registry_server(provider, host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
