from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from clawcu.a2a.card import AgentCard, display_port_for_record
from clawcu.a2a.registry import try_fetch_plugin_card


def detect_plugin_or_none(
    record: Any,
    *,
    service: Any,
    host: str = "127.0.0.1",
    timeout: float = 0.5,
    attempts: int = 3,
    retry_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> AgentCard | None:
    """Probe a record's display port for a plugin AgentCard.

    Retries a small number of times because an instance that just started
    may not have wired its /.well-known route yet. Returns the card on the
    first success, ``None`` after all attempts fail.
    """
    total = max(1, attempts)
    for i in range(total):
        card = try_fetch_plugin_card(record, service=service, host=host, timeout=timeout)
        if card is not None:
            return card
        if i < total - 1 and retry_delay > 0:
            sleep(retry_delay)
    return None


def describe_probe(record: Any, *, service: Any, host: str = "127.0.0.1") -> str:
    port = display_port_for_record(record, service=service)
    return f"http://{host}:{port}/.well-known/agent-card.json"


def _probe_family(family: int, addr: tuple, timeout: float) -> bool:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex(addr) == 0
    except OSError:
        return False


def port_already_bound(port: int, *, timeout: float = 0.1) -> bool:
    """Return True if anything already accepts TCP connects on ``port``.

    Checks both IPv4 and IPv6 localhost concurrently. Docker-for-Mac binds
    ``*:PORT`` on IPv6, while ``ThreadingHTTPServer(("127.0.0.1", PORT), ...)``
    succeeds on IPv4 even when that IPv6 socket is live — the two address
    families do not collide. Probing both prevents that shadowing.

    Review-1 §13: families are probed in parallel so the fresh-port case
    (both probes time out) costs one ``timeout`` rather than two. Saves
    ~timeout × (N-1) per ``a2a up`` with N managed instances.
    """
    targets: tuple[tuple[int, tuple], ...] = (
        (socket.AF_INET, ("127.0.0.1", port)),
        (socket.AF_INET6, ("::1", port, 0, 0)),
    )
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(_probe_family, f, a, timeout) for f, a in targets]
        for fut in as_completed(futures):
            if fut.result():
                return True
    return False
