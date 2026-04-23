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
    plugin_port_candidates,
)
from clawcu.a2a.sidecar_plugin import resolve_advertise_host

CardProvider = Callable[[], Iterable[AgentCard]]

DEFAULT_PLUGIN_FETCH_TIMEOUT = 0.5
DEFAULT_CARDS_TTL_SECONDS = 5.0

_log = logging.getLogger("clawcu.a2a.registry")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return None from redirect_request so urllib surfaces 3xx as an
    HTTPError instead of following.

    Review-20 P1-L1: registry federation probes loopback plugin ports
    for their ``/.well-known/agent-card.json``. A compromised plugin
    could return ``302 Location: ftp://attacker/`` — CPython's default
    redirect handler would follow it, giving the attacker an egress
    from the registry process. Disabling redirects keeps card fetches
    pinned to the trusted http/https URL that we constructed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


A2A_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _ResponseTooLarge(Exception):
    """Raised when a plugin card response exceeds ``A2A_MAX_RESPONSE_BYTES``.

    Review-21 P2-M1: a compromised plugin (or anything squatting on a
    probed port) could stream GBs as its card and OOM the registry
    process. Cap is a compile-time constant.
    """


def _read_capped(response, cap: int = A2A_MAX_RESPONSE_BYTES) -> bytes:
    raw = response.read(cap + 1)
    if len(raw) > cap:
        raise _ResponseTooLarge(f"response exceeds {cap} bytes")
    return raw


def _fetch_card_at(url: str, timeout: float) -> AgentCard | None:
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            if response.status != 200:
                _log.info(
                    "plugin card fetch non-200: url=%s status=%s", url, response.status
                )
                return None
            raw = _read_capped(response)
    except urllib.error.HTTPError as exc:
        _log.info("plugin card fetch http error: url=%s status=%s", url, exc.code)
        return None
    except _ResponseTooLarge as exc:
        _log.info("plugin card fetch too large: url=%s reason=%s", url, exc)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Review-1 §6: connection-refused/timeout/DNS failure is a normal
        # "instance not up" negative during `a2a up` probing; logging at
        # INFO floods 10+ lines when several stopped instances are in the
        # registry. HTTP non-200, bad JSON, bad schema remain INFO — those
        # mean the port *did* respond but the card is malformed.
        _log.debug("plugin card fetch failed: url=%s reason=%s", url, exc)
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


def try_fetch_plugin_card(
    record: Any,
    *,
    service: Any = None,
    host: str = "127.0.0.1",
    timeout: float = DEFAULT_PLUGIN_FETCH_TIMEOUT,
) -> AgentCard | None:
    """Best-effort GET of a running plugin's self-reported AgentCard.

    Probes each candidate port in order (display_port, then the sidecar slot
    for services that run the plugin next to a gateway); first successful
    fetch wins. Returns ``None`` when every candidate fails so the caller
    can fall back to a service-type placeholder. Per-candidate failures log
    at INFO with the URL + reason; success is silent.
    """
    for port in plugin_port_candidates(record, service=service):
        url = f"http://{host}:{port}/.well-known/agent-card.json"
        card = _fetch_card_at(url, timeout)
        if card is not None:
            return card
    return None


_FEDERATABLE_STATUSES = {"running", "starting"}


def _build_cards(service: Any, host: str, timeout: float) -> list[AgentCard]:
    # We deliberately don't use `running_only=True` — container_status maps
    # docker healthcheck phase "starting" to status="starting", and a fresh
    # instance can sit in that state for a full healthcheck interval (up to
    # 180s with the stock openclaw image). The A2A sidecar binds as soon as
    # the process starts, independent of gateway readiness, so we should
    # probe it immediately rather than wait for the first healthcheck tick
    # (review-10 P2-A4).
    records = [
        r
        for r in service.list_instances()
        # Records in tests (and any custom service adapter) may not expose
        # `.status` — default to "running" so legacy callers that already
        # pre-filtered their list_instances() output stay opt-in.
        if getattr(r, "status", "running") in _FEDERATABLE_STATUSES
    ]
    out: list[AgentCard] = []
    for record in records:
        card = try_fetch_plugin_card(
            record, service=service, host=host, timeout=timeout
        )
        if card is not None:
            out.append(card)
            continue
        # Review-12 P2-D2: when the probe fails, distinguish by status.
        # A ``running`` record has passed at least one healthcheck, so a
        # probe miss is most likely a transient network hiccup — keep the
        # placeholder card so peers can still discover it and retry
        # through forward_to_peer's own error handling. A ``starting``
        # record has NOT yet passed a healthcheck; publishing a
        # placeholder endpoint that actually 504s gives peers a worse
        # experience than admitting the sidecar isn't ready yet. Skip it
        # and let the 5 s cache TTL pick it up on the next pass.
        status = getattr(record, "status", "running")
        if status == "starting":
            _log.info(
                "skipping card for starting instance %s: sidecar probe failed",
                getattr(record, "name", "<unknown>"),
            )
            continue
        # Review-13 P2-E1: the placeholder endpoint must be reachable by
        # peers on the mesh, not just by the CLI on the host loopback.
        # `host` here is the interface the registry binds to (typically
        # 127.0.0.1 in local clawcu setups) — that host is NOT valid
        # inside a peer container (its own loopback). Use the record's
        # advertise host (Darwin/Windows → host.docker.internal, Linux →
        # 127.0.0.1 unless overridden) so `forward_to_peer` from another
        # container actually reaches this instance. The CLI path still
        # works because iter-11 P1-C1's localize_endpoint_for_host
        # rewrites host.docker.internal → 127.0.0.1 before posting.
        placeholder_host = resolve_advertise_host(record)
        out.append(
            card_from_record(record, service=service, host=placeholder_host)
        )
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
