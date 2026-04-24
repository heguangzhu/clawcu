"""pytest port of tests/sidecar_outbound.test.js."""
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
from _common.mcp import UpstreamError  # noqa: E402


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start(handler_factory):
    """Spin up a stdlib HTTP server on 127.0.0.1 with an ephemeral port."""
    handler_cls = handler_factory()
    srv = _ThreadedHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}"


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def _silent_handler(handle_fn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # silence stderr default
            return

    # Attach a polymorphic do_GET/do_POST delegating to handle_fn.
    def do_any(self):
        handle_fn(self)

    H.do_GET = do_any  # noqa: E305
    H.do_POST = do_any
    return H


# -- lookup_peer --------------------------------------------------------------


def test_lookup_peer_returns_card_on_200():
    card = {
        "name": "analyst",
        "role": "hermes",
        "skills": ["chat"],
        "endpoint": "http://127.0.0.1:9129/a2a/send",
    }

    def handle(h):
        assert h.path == "/agents/analyst"
        body = json.dumps(card).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        got = sidecar.lookup_peer(registry_url=url, peer_name="analyst", timeout_ms=2000)
        assert got == card
    finally:
        _stop(srv)


def test_lookup_peer_404_surfaces_http_status_404():
    def handle(h):
        body = json.dumps({"error": "not_found"}).encode("utf-8")
        h.send_response(404)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        with pytest.raises(UpstreamError) as exc_info:
            sidecar.lookup_peer(registry_url=url, peer_name="missing", timeout_ms=2000)
        assert exc_info.value.http_status == 404
        assert "not found" in str(exc_info.value)
    finally:
        _stop(srv)


def test_lookup_peer_non_2xx_non_404_surfaces_http_status_503():
    def handle(h):
        h.send_response(500)
        h.send_header("content-length", "4")
        h.end_headers()
        h.wfile.write(b"boom")

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        with pytest.raises(UpstreamError) as exc_info:
            sidecar.lookup_peer(registry_url=url, peer_name="analyst", timeout_ms=2000)
        assert exc_info.value.http_status == 503
    finally:
        _stop(srv)


def test_lookup_peer_missing_endpoint_surfaces_http_status_503():
    def handle(h):
        body = json.dumps({"name": "analyst"}).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        with pytest.raises(UpstreamError) as exc_info:
            sidecar.lookup_peer(registry_url=url, peer_name="analyst", timeout_ms=2000)
        assert exc_info.value.http_status == 503
        assert "endpoint" in str(exc_info.value)
    finally:
        _stop(srv)


# -- forward_to_peer ---------------------------------------------------------


def test_forward_to_peer_200_returns_parsed_body_and_carries_hop_header():
    observed = {"hop": None, "body": None}

    def handle(h):
        length = int(h.headers.get("content-length") or "0")
        raw = h.rfile.read(length)
        observed["hop"] = h.headers.get("x-a2a-hop")
        observed["body"] = json.loads(raw.decode("utf-8"))
        resp = json.dumps({"from": "analyst", "reply": "42", "thread_id": None}).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(resp)))
        h.end_headers()
        h.wfile.write(resp)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        got = sidecar.forward_to_peer(
            endpoint=f"{url}/a2a/send",
            self_name="writer",
            peer_name="analyst",
            message="hi",
            thread_id=None,
            hop=3,
            timeout_ms=2000,
        )
        assert got["reply"] == "42"
        assert observed["hop"] == "3"
        assert observed["body"]["from"] == "writer"
        assert observed["body"]["to"] == "analyst"
        assert observed["body"]["message"] == "hi"
        assert "thread_id" not in observed["body"]
    finally:
        _stop(srv)


def test_forward_to_peer_thread_id_propagates_when_present():
    observed = {"body": None}

    def handle(h):
        length = int(h.headers.get("content-length") or "0")
        raw = h.rfile.read(length)
        observed["body"] = json.loads(raw.decode("utf-8"))
        resp = json.dumps({"from": "analyst", "reply": "k", "thread_id": "t-1"}).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(resp)))
        h.end_headers()
        h.wfile.write(resp)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        sidecar.forward_to_peer(
            endpoint=f"{url}/a2a/send",
            self_name="writer",
            peer_name="analyst",
            message="hi",
            thread_id="t-1",
            hop=1,
            timeout_ms=2000,
        )
        assert observed["body"]["thread_id"] == "t-1"
    finally:
        _stop(srv)


def test_forward_to_peer_508_surfaces_http_status_508():
    def handle(h):
        body = json.dumps({"error": "hop budget exceeded"}).encode("utf-8")
        h.send_response(508)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        with pytest.raises(UpstreamError) as exc_info:
            sidecar.forward_to_peer(
                endpoint=f"{url}/a2a/send",
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=9,
                timeout_ms=2000,
            )
        assert exc_info.value.http_status == 508
    finally:
        _stop(srv)


def test_forward_to_peer_500_maps_to_http_status_502():
    def handle(h):
        h.send_response(500)
        h.send_header("content-length", "4")
        h.end_headers()
        h.wfile.write(b"boom")

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        with pytest.raises(UpstreamError) as exc_info:
            sidecar.forward_to_peer(
                endpoint=f"{url}/a2a/send",
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=1,
                timeout_ms=2000,
            )
        assert exc_info.value.http_status == 502
        assert exc_info.value.peer_status == 500
    finally:
        _stop(srv)


# -- network-layer failures map to 504 --------------------------------------


def test_forward_to_peer_connection_refused_maps_to_504():
    # Bind + close to leak a port that is definitely unused.
    srv, url = _start(lambda: _silent_handler(lambda h: None))
    _stop(srv)
    with pytest.raises(UpstreamError) as exc_info:
        sidecar.forward_to_peer(
            endpoint=f"{url}/a2a/send",
            self_name="writer",
            peer_name="analyst",
            message="hi",
            thread_id=None,
            hop=1,
            timeout_ms=2000,
        )
    assert exc_info.value.http_status == 504


def test_forward_to_peer_request_timeout_maps_to_504():
    stop_evt = threading.Event()

    def handle(h):
        # Read the body so the client completes its write side, then hang.
        length = int(h.headers.get("content-length") or "0")
        if length > 0:
            h.rfile.read(length)
        stop_evt.wait(timeout=5.0)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        with pytest.raises(UpstreamError) as exc_info:
            sidecar.forward_to_peer(
                endpoint=f"{url}/a2a/send",
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=1,
                timeout_ms=200,
            )
        assert exc_info.value.http_status == 504
    finally:
        stop_evt.set()
        _stop(srv)


# -- read_hop_header --------------------------------------------------------


def test_read_hop_header_absent_returns_zero():
    assert sidecar.read_hop_header({}) == 0


def test_read_hop_header_valid_integer_parses():
    assert sidecar.read_hop_header({"x-a2a-hop": "3"}) == 3


def test_read_hop_header_negative_rejected_zero():
    assert sidecar.read_hop_header({"x-a2a-hop": "-1"}) == 0


def test_read_hop_header_garbage_zero():
    assert sidecar.read_hop_header({"x-a2a-hop": "abc"}) == 0


def test_read_hop_header_float_truncates_to_int():
    assert sidecar.read_hop_header({"x-a2a-hop": "2.9"}) == 2


# -- parse_http_url ---------------------------------------------------------


def test_parse_http_url_default_port_80_for_http():
    p = sidecar.parse_http_url("http://host.docker.internal/agents")
    assert p["host"] == "host.docker.internal"
    assert p["port"] == 80


def test_parse_http_url_explicit_port_wins():
    p = sidecar.parse_http_url("http://127.0.0.1:8765")
    assert p["port"] == 8765
    assert p["pathname"] == "/"


def test_parse_http_url_rejects_file_scheme():
    # Shared _common/peer_cache.validate_outbound_url is the source of
    # truth for the scheme allow-list; parse_http_url surfaces its reason
    # via RuntimeError. Match on the scheme-rejection phrase rather than
    # the exact wrapper to tolerate future message tweaks upstream.
    with pytest.raises(RuntimeError, match="scheme 'file' not allowed"):
        sidecar.parse_http_url("file:///etc/hosts")


# -- request_id correlation -------------------------------------------------


def test_read_or_mint_request_id_uses_caller_supplied_header_when_valid():
    assert sidecar.read_or_mint_request_id({"x-a2a-request-id": "abc-123"}) == "abc-123"


def test_read_or_mint_request_id_trims_whitespace():
    assert sidecar.read_or_mint_request_id({"x-a2a-request-id": "  zzz  "}) == "zzz"


def test_read_or_mint_request_id_mints_fresh_when_missing():
    rid = sidecar.read_or_mint_request_id({})
    assert isinstance(rid, str)
    assert len(rid) >= 16


def test_read_or_mint_request_id_rejects_control_chars_and_mints():
    rid = sidecar.read_or_mint_request_id({"x-a2a-request-id": "bad\nvalue"})
    assert rid != "bad\nvalue"
    assert isinstance(rid, str)


def test_read_or_mint_request_id_rejects_over_128_chars():
    too_big = "x" * 200
    rid = sidecar.read_or_mint_request_id({"x-a2a-request-id": too_big})
    assert rid != too_big
    assert len(rid) <= 64


def test_forward_to_peer_forwards_x_a2a_request_id_header():
    observed = {"rid": "uninitialized"}

    def handle(h):
        length = int(h.headers.get("content-length") or "0")
        if length > 0:
            h.rfile.read(length)
        observed["rid"] = h.headers.get("x-a2a-request-id")
        body = json.dumps({"from": "peer", "reply": "ok", "thread_id": None}).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        sidecar.forward_to_peer(
            endpoint=f"{url}/a2a/send",
            self_name="caller",
            peer_name="peer",
            message="hi",
            thread_id=None,
            hop=1,
            timeout_ms=2000,
            request_id="corr-42",
        )
        assert observed["rid"] == "corr-42"
    finally:
        _stop(srv)


def test_forward_to_peer_omits_request_id_header_when_none():
    observed = {"rid": "uninitialized"}

    def handle(h):
        length = int(h.headers.get("content-length") or "0")
        if length > 0:
            h.rfile.read(length)
        observed["rid"] = h.headers.get("x-a2a-request-id")
        body = json.dumps({"from": "peer", "reply": "ok", "thread_id": None}).encode("utf-8")
        h.send_response(200)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _start(lambda: _silent_handler(handle))
    try:
        sidecar.forward_to_peer(
            endpoint=f"{url}/a2a/send",
            self_name="caller",
            peer_name="peer",
            message="hi",
            thread_id=None,
            hop=1,
            timeout_ms=2000,
        )
        assert observed["rid"] is None
    finally:
        _stop(srv)
