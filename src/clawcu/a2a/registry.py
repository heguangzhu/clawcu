from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

from clawcu.a2a.card import (
    AgentCard,
    card_from_record,
    display_port_for_record,
)

CardProvider = Callable[[], Iterable[AgentCard]]

DEFAULT_PLUGIN_FETCH_TIMEOUT = 0.5
DEFAULT_CARDS_TTL_SECONDS = 5.0

_log = logging.getLogger("clawcu.a2a.registry")


def try_fetch_plugin_card(
    record: Any,
    *,
    service: Any = None,
    host: str = "127.0.0.1",
    timeout: float = DEFAULT_PLUGIN_FETCH_TIMEOUT,
) -> AgentCard | None:
    """Best-effort GET of a running plugin's self-reported AgentCard.

    Returns ``None`` on any failure (timeout, non-200, non-JSON, schema
    mismatch) so the caller can fall back to a service-type placeholder.
    Failures log at INFO with the URL + reason; success is silent.
    """
    port = display_port_for_record(record, service=service)
    url = f"http://{host}:{port}/.well-known/agent-card.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                _log.info(
                    "plugin card fetch non-200: url=%s status=%s", url, response.status
                )
                return None
            raw = response.read()
    except urllib.error.HTTPError as exc:
        _log.info("plugin card fetch http error: url=%s status=%s", url, exc.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log.info("plugin card fetch failed: url=%s reason=%s", url, exc)
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.info("plugin card fetch bad json: url=%s reason=%s", url, exc)
        return None
    try:
        return AgentCard.from_dict(payload)
    except (ValueError, TypeError, AttributeError) as exc:
        _log.info("plugin card fetch bad schema: url=%s reason=%s", url, exc)
        return None


def _build_cards(service: Any, host: str, timeout: float) -> list[AgentCard]:
    records = service.list_instances(running_only=True)
    out: list[AgentCard] = []
    for record in records:
        card = try_fetch_plugin_card(
            record, service=service, host=host, timeout=timeout
        )
        if card is None:
            card = card_from_record(record, service=service, host=host)
        out.append(card)
    return out


def cards_from_service(
    service: Any,
    *,
    host: str = "127.0.0.1",
    timeout: float = DEFAULT_PLUGIN_FETCH_TIMEOUT,
) -> list[AgentCard]:
    """One-shot federation pass. Use ``make_cards_provider`` for a TTL-cached view."""
    return _build_cards(service, host, timeout)


def make_cards_provider(
    service: Any,
    *,
    host: str = "127.0.0.1",
    timeout: float = DEFAULT_PLUGIN_FETCH_TIMEOUT,
    ttl: float = DEFAULT_CARDS_TTL_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> CardProvider:
    """Return a zero-arg provider with a TTL cache bound to this closure.

    The cache lives on the returned closure (no module-level globals), so
    multiple registries in the same process — e.g. tests — do not leak state
    into each other. ``ttl <= 0`` disables caching.
    """
    state: dict[str, Any] = {"cards": None, "expires": 0.0}
    lock = threading.Lock()

    def provider() -> list[AgentCard]:
        if ttl <= 0:
            return _build_cards(service, host, timeout)
        with lock:
            if state["cards"] is not None and now() < state["expires"]:
                return list(state["cards"])
            cards = _build_cards(service, host, timeout)
            state["cards"] = cards
            state["expires"] = now() + ttl
            return list(cards)

    return provider


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
