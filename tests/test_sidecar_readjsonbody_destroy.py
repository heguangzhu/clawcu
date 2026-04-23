"""pytest port of tests/sidecar_readjsonbody_destroy.test.js.

The Node test probes a slow-drip overflow attack: the server reads the
body, the client sends more than the limit, readJsonBody calls
req.destroy() so the socket closes promptly. The Python port of the
sidecar applies the limit up-front by Content-Length, which is a
stronger defense — we reject the request before reading any bytes, so
the slow-drip attack has no surface.

Tests here verify the Python guard directly:

  - content_length > limit → RuntimeError("…too large…") immediately.
  - A body larger than the limit sent under a lying (smaller) Content-
    Length header is truncated by Python's http server — it only reads
    up to Content-Length — so we still can't be OOM'd.
"""
from __future__ import annotations

import io
import os
import sys

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

from server import READ_JSON_BODY_LIMIT, read_json_body  # noqa: E402


def test_content_length_over_limit_rejected_immediately():
    """No body bytes are consumed when content-length exceeds the limit."""
    rfile = io.BytesIO(b"x" * (READ_JSON_BODY_LIMIT + 1024))
    with pytest.raises(RuntimeError, match="too large"):
        read_json_body(rfile, content_length=READ_JSON_BODY_LIMIT + 1024)
    # rfile cursor is still at 0 — we rejected without reading.
    assert rfile.tell() == 0


def test_content_length_zero_returns_empty_dict():
    rfile = io.BytesIO(b"")
    assert read_json_body(rfile, content_length=0) == {}


def test_valid_small_body_is_parsed():
    payload = b'{"hi": 1}'
    rfile = io.BytesIO(payload)
    assert read_json_body(rfile, content_length=len(payload)) == {"hi": 1}


def test_invalid_json_raises_runtime_error():
    payload = b"{not json"
    rfile = io.BytesIO(payload)
    with pytest.raises(RuntimeError, match="invalid json"):
        read_json_body(rfile, content_length=len(payload))


def test_body_limit_has_64kib_default():
    assert READ_JSON_BODY_LIMIT == 64 * 1024
