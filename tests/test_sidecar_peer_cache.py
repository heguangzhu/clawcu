"""pytest port of tests/sidecar_peer_cache.test.js."""
from __future__ import annotations

import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_SIDECAR = os.path.abspath(
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
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

import server as sidecar  # noqa: E402


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start(handle_fn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):  # noqa: N802
            handle_fn(self)

    srv = _ThreadedHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _stop(srv):
    srv.shutdown()
    srv.server_close()


# -- fetch_peer_list ---------------------------------------------------------


def test_fetch_peer_list_returns_array_on_200():
    def handle(h):
        assert h.path == "/agents"
        body = json.dumps(
            [
                {"name": "a", "role": "r", "skills": ["s"]},
                {"name": "b", "role": "r2", "skills": []},
            ]
        ).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(handle)
    try:
        peers = sidecar.fetch_peer_list(registry_url=url, timeout_ms=2000)
        assert len(peers) == 2
        assert peers[0]["name"] == "a"
    finally:
        _stop(srv)


def test_fetch_peer_list_returns_none_on_404():
    def handle(h):
        h.send_response(404)
        h.send_header("content-length", "0")
        h.end_headers()

    srv, url = _start(handle)
    try:
        peers = sidecar.fetch_peer_list(registry_url=url, timeout_ms=2000)
        assert peers is None
    finally:
        _stop(srv)


def test_fetch_peer_list_returns_none_on_non_json_body():
    def handle(h):
        body = b"not json"
        h.send_response(200)
        h.send_header("content-type", "text/plain")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(handle)
    try:
        peers = sidecar.fetch_peer_list(registry_url=url, timeout_ms=2000)
        assert peers is None
    finally:
        _stop(srv)


def test_fetch_peer_list_returns_none_on_non_array_response():
    def handle(h):
        body = b'{"peers":[]}'
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(handle)
    try:
        peers = sidecar.fetch_peer_list(registry_url=url, timeout_ms=2000)
        assert peers is None
    finally:
        _stop(srv)


def test_fetch_peer_list_filters_entries_without_a_name():
    def handle(h):
        body = json.dumps(
            [{"name": "a"}, {"role": "r"}, None, {"name": "c"}]
        ).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(handle)
    try:
        peers = sidecar.fetch_peer_list(registry_url=url, timeout_ms=2000)
        assert [p["name"] for p in peers] == ["a", "c"]
    finally:
        _stop(srv)


# -- create_peer_cache -------------------------------------------------------


def test_create_peer_cache_serves_cached_within_ttl():
    calls = {"n": 0}

    def fetch(registry_url, timeout_ms):
        calls["n"] += 1
        return [{"name": "a"}]

    cache = sidecar.create_peer_cache(
        registry_url="http://stub",
        timeout_ms=2000,
        fresh_ms=30_000,
        now_fn=lambda: 1000,
        fetch_fn=fetch,
    )
    r1 = cache["get"]()
    r2 = cache["get"]()
    assert calls["n"] == 1
    assert r1 == r2


def test_create_peer_cache_refetches_after_ttl_expires():
    calls = {"n": 0}
    now = {"v": 1000}

    def fetch(registry_url, timeout_ms):
        calls["n"] += 1
        return [{"name": "a"}]

    cache = sidecar.create_peer_cache(
        registry_url="http://stub",
        timeout_ms=2000,
        fresh_ms=30_000,
        now_fn=lambda: now["v"],
        fetch_fn=fetch,
    )
    cache["get"]()
    now["v"] += 31_000
    cache["get"]()
    assert calls["n"] == 2


def test_create_peer_cache_serves_stale_on_fetch_failure_inside_stale_window():
    calls = {"n": 0}
    now = {"v": 1000}

    def fetch(registry_url, timeout_ms):
        calls["n"] += 1
        return [{"name": "a"}] if calls["n"] == 1 else None

    cache = sidecar.create_peer_cache(
        registry_url="http://stub",
        timeout_ms=2000,
        fresh_ms=30_000,
        stale_ms=300_000,
        now_fn=lambda: now["v"],
        fetch_fn=fetch,
    )
    r1 = cache["get"]()
    assert r1 == [{"name": "a"}]
    now["v"] += 60_000
    r2 = cache["get"]()
    assert r2 == [{"name": "a"}]


def test_create_peer_cache_returns_none_after_stale_window():
    now = {"v": 1000}

    def fetch(registry_url, timeout_ms):
        return [{"name": "a"}] if now["v"] == 1000 else None

    cache = sidecar.create_peer_cache(
        registry_url="http://stub",
        timeout_ms=2000,
        fresh_ms=30_000,
        stale_ms=300_000,
        now_fn=lambda: now["v"],
        fetch_fn=fetch,
    )
    cache["get"]()
    now["v"] += 400_000
    r = cache["get"]()
    assert r is None


def test_create_peer_cache_dedupes_concurrent_fetches():
    """Under a single lock, concurrent get()s must funnel to one fetch."""
    calls = {"n": 0}
    enter_evt = threading.Event()

    def fetch(registry_url, timeout_ms):
        calls["n"] += 1
        # Sleep a bit so other threads hit the cache freshness on return.
        enter_evt.wait(timeout=0.1)
        return [{"name": "a"}]

    cache = sidecar.create_peer_cache(
        registry_url="http://stub",
        timeout_ms=2000,
        fresh_ms=30_000,
        now_fn=lambda: 1000,
        fetch_fn=fetch,
    )
    results = [None, None, None]

    def runner(i):
        results[i] = cache["get"]()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    enter_evt.set()
    for t in threads:
        t.join()
    # Under Python's lock + single-threaded now_fn, once the first fetch
    # completes and populates `cached`, subsequent calls within the same
    # `now` value short-circuit on freshness — so fetch runs once.
    assert calls["n"] == 1
    assert results[0] == results[1] == results[2] == [{"name": "a"}]
