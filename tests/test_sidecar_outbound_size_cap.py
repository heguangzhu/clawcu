"""pytest port of tests/sidecar_outbound_size_cap.test.js.

Guards the outbound response body cap: lookup_peer / forward_to_peer /
fetch_peer_list must refuse to buffer responses larger than
A2A_MAX_RESPONSE_BYTES so a compromised peer or registry can't OOM the
sidecar.
"""
from __future__ import annotations

import os
import socketserver
import sys
import threading
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
from mcp import UpstreamError  # noqa: E402


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_oversized_server():
    oversize = 5 * 1024 * 1024  # > 4 MiB cap

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def _drain_body(self):
            length = int(self.headers.get("content-length") or "0")
            if length > 0:
                try:
                    self.rfile.read(length)
                except Exception:
                    pass

        def _pump_oversize(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(oversize))
            self.end_headers()
            chunk = b"a" * 65536
            sent = 0
            while sent < oversize:
                n = min(len(chunk), oversize - sent)
                try:
                    self.wfile.write(chunk[:n])
                    sent += n
                except (BrokenPipeError, ConnectionResetError):
                    return

        def do_GET(self):  # noqa: N802
            self._pump_oversize()

        def do_POST(self):  # noqa: N802
            self._drain_body()
            self._pump_oversize()

    srv = _ThreadedHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def test_lookup_peer_rejects_oversized_registry_response():
    srv, port = _start_oversized_server()
    try:
        # http_request_raw re-raises the cap overflow as RuntimeError; that
        # bubbles out of lookup_peer unwrapped. The key guarantee is that we
        # refused to buffer the oversized body — not the exception class.
        with pytest.raises((UpstreamError, RuntimeError), match="exceeds"):
            sidecar.lookup_peer(
                registry_url=f"http://127.0.0.1:{port}",
                peer_name="analyst",
                timeout_ms=5000,
            )
    finally:
        _stop(srv)


def test_forward_to_peer_rejects_oversized_peer_response():
    srv, port = _start_oversized_server()
    try:
        # post_json raises RuntimeError("…exceeds…"); forward_to_peer catches
        # and wraps as UpstreamError("peer unreachable or timed out: …"). Either
        # surfacing is acceptable — the guarantee is we did not buffer the body.
        with pytest.raises((UpstreamError, RuntimeError), match="exceeds|unreachable"):
            sidecar.forward_to_peer(
                endpoint=f"http://127.0.0.1:{port}/a2a/send",
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=1,
                timeout_ms=5000,
            )
    finally:
        _stop(srv)


def test_max_response_bytes_is_4_mib():
    assert sidecar.A2A_MAX_RESPONSE_BYTES == 4 * 1024 * 1024
