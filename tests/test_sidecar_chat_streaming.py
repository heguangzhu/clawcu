"""Streaming chat-completions consumer tests (a2a-async layer 3).

Covers the SSE-decoding helpers added to both sidecars so a long-running
upstream LLM call surfaces live progress instead of one opaque blob between
the "calling gateway" and "received reply" task breadcrumbs.

Hermes side: ``gateway._consume_chat_stream_from`` — feeds a synthetic SSE
iterable in directly (no socket required) so the assertion focuses on the
parser + progress-throttling behaviour.

Openclaw side: ``chat.post_chat_completion_streaming`` — stubs
``_connection_for`` with a fake HTTPConnection that returns a canned SSE
response, so the assertion covers both the request shape (stream=true,
SSE accept header) and the streaming-read loop.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Hermes gateway streaming consumer
# ---------------------------------------------------------------------------

_HERMES_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "src", "clawcu", "a2a", "sidecar_plugin", "hermes", "sidecar",
    )
)
_COMMON_PARENT = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "src", "clawcu", "a2a", "sidecar_plugin",
    )
)


def _load_hermes_gateway():
    paths = []
    for p in (_HERMES_DIR, _COMMON_PARENT):
        if p not in sys.path:
            sys.path.insert(0, p)
            paths.append(p)
    spec = importlib.util.spec_from_file_location(
        "hermes_sidecar_gateway", os.path.join(_HERMES_DIR, "gateway.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for p in paths:
        try:
            sys.path.remove(p)
        except ValueError:
            pass
    sys.modules.pop("config", None)
    return mod


hermes_gateway = _load_hermes_gateway()


class _FakeSSEResponse:
    """Iterable byte-line stand-in for an HTTPResponse.

    ``between`` (optional) runs after each yielded line — useful to mutate a
    test clock between deltas so we cross the progress-throttle boundary.
    """

    def __init__(self, lines: list[bytes], *, between=None):
        self._lines = lines
        self._between = between
        self.closed = False

    def __iter__(self):
        for i, line in enumerate(self._lines):
            yield line
            if self._between is not None:
                self._between(i)

    def close(self) -> None:
        self.closed = True


def _sse(*deltas: str, done: bool = True) -> list[bytes]:
    out: list[bytes] = []
    for d in deltas:
        evt = (
            '{"choices":[{"delta":{"content":"' + d.replace('"', '\\"') + '"}}]}'
        )
        out.append(("data: " + evt + "\n").encode("utf-8"))
        out.append(b"\n")
    if done:
        out.append(b"data: [DONE]\n")
    return out


def test_hermes_stream_concatenates_deltas_and_emits_progress(monkeypatch):
    fake_now = [1000.0]

    def now():
        return fake_now[0]

    monkeypatch.setattr(hermes_gateway.time, "monotonic", now)

    notes: list[str] = []

    def advance(idx: int) -> None:
        if idx == 2:
            fake_now[0] += 5.0

    resp = _FakeSSEResponse(
        _sse("Hello", " world", "!", " how are you?"),
        between=advance,
    )

    text = hermes_gateway._consume_chat_stream_from(
        resp,
        progress=notes.append,
        progress_interval_s=3.0,
    )

    assert text == "Hello world! how are you?"
    assert resp.closed is True
    assert len(notes) >= 1
    note = notes[-1]
    assert note.startswith("streaming: ")
    assert " chars · …" in note
    # The tail must be a (possibly partial) suffix of what had been
    # accumulated by the time the throttle fired — at minimum, "Hello".
    assert "Hello" in note


def test_hermes_stream_skips_keep_alive_and_bad_lines(monkeypatch):
    monkeypatch.setattr(hermes_gateway.time, "monotonic", lambda: 0.0)
    notes: list[str] = []

    lines = [
        b": keep-alive comment\n",
        b"\n",
        b'data: {"choices":[{"delta":{"content":"A"}}]}\n',
        b"data: not-json\n",
        b'data: {"choices":[{"delta":{}}]}\n',
        b'data: {"choices":[{"delta":{"content":"B"}}]}\n',
        b"data: [DONE]\n",
    ]
    text = hermes_gateway._consume_chat_stream_from(
        _FakeSSEResponse(lines),
        progress=notes.append,
        progress_interval_s=3.0,
    )
    assert text == "AB"


def test_hermes_stream_caps_total_size(monkeypatch):
    monkeypatch.setattr(hermes_gateway.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(hermes_gateway, "A2A_LOCAL_UPSTREAM_CAP", 4)
    lines = _sse("abc", "def")  # 6 bytes — exceeds cap of 4
    with pytest.raises(RuntimeError, match="exceeded"):
        hermes_gateway._consume_chat_stream_from(
            _FakeSSEResponse(lines),
            progress=lambda _msg: None,
            progress_interval_s=3.0,
        )


def test_hermes_stream_progress_callback_failures_are_swallowed(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr(hermes_gateway.time, "monotonic", lambda: fake_now[0])

    def boom(_msg):
        raise RuntimeError("downstream broken")

    def advance(_idx):
        fake_now[0] += 5.0

    resp = _FakeSSEResponse(_sse("hi", " there"), between=advance)
    text = hermes_gateway._consume_chat_stream_from(
        resp,
        progress=boom,
        progress_interval_s=3.0,
    )
    assert text == "hi there"


# ---------------------------------------------------------------------------
# Openclaw post_chat_completion_streaming
# ---------------------------------------------------------------------------

_OPENCLAW_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "src", "clawcu", "a2a", "sidecar_plugin", "openclaw", "sidecar",
    )
)


def _load_openclaw_chat():
    paths = []
    for p in (_OPENCLAW_DIR, _COMMON_PARENT):
        if p not in sys.path:
            sys.path.insert(0, p)
            paths.append(p)
    # http_client must be importable as a top-level module so chat.py's
    # `from http_client import …` resolves.
    import importlib
    if "http_client" in sys.modules:
        del sys.modules["http_client"]
    spec = importlib.util.spec_from_file_location(
        "http_client", os.path.join(_OPENCLAW_DIR, "http_client.py")
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    sys.modules["http_client"] = hc

    spec = importlib.util.spec_from_file_location(
        "openclaw_sidecar_chat", os.path.join(_OPENCLAW_DIR, "chat.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for p in paths:
        try:
            sys.path.remove(p)
        except ValueError:
            pass
    return mod, hc


openclaw_chat, openclaw_http_client = _load_openclaw_chat()


class _FakeResp:
    def __init__(self, status: int, lines: list[bytes], *, between=None):
        self.status = status
        self._lines = lines
        self._between = between
        self._buffer = b"".join(lines)

    def __iter__(self):
        for i, line in enumerate(self._lines):
            yield line
            if self._between is not None:
                self._between(i)

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._buffer
        head, self._buffer = self._buffer[:n], self._buffer[n:]
        return head


class _FakeConn:
    last: "_FakeConn | None" = None

    def __init__(self, status: int, lines: list[bytes], *, between=None):
        self._resp = _FakeResp(status, lines, between=between)
        self.requests: list[tuple] = []
        self.closed = False
        _FakeConn.last = self

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return self._resp

    def close(self):
        self.closed = True


def test_openclaw_streaming_returns_concatenated_text(monkeypatch):
    monkeypatch.setattr(openclaw_chat.time, "monotonic", lambda: 0.0)
    fake = _FakeConn(200, _sse("foo", " bar"))
    monkeypatch.setattr(
        openclaw_chat,
        "_connection_for",
        lambda host, port, timeout_s, scheme="http": fake,
    )

    notes: list[str] = []
    text = openclaw_chat.post_chat_completion_streaming(
        gateway_host="127.0.0.1",
        gateway_port=12345,
        token="tok",
        user_message="ping",
        system_prompt="sys",
        history=[],
        model="openclaw",
        timeout_ms=5000,
        progress=notes.append,
        progress_interval_s=3.0,
    )

    assert text == "foo bar"
    assert fake.closed is True

    method, path, body, headers = fake.requests[0]
    assert method == "POST"
    assert path == "/v1/chat/completions"
    assert headers["accept"] == "text/event-stream"
    assert headers["authorization"] == "Bearer tok"
    import json as _json
    parsed_body = _json.loads(body)
    assert parsed_body["stream"] is True
    assert parsed_body["model"] == "openclaw"
    assert parsed_body["messages"][0] == {"role": "system", "content": "sys"}
    assert parsed_body["messages"][-1] == {"role": "user", "content": "ping"}


def test_openclaw_streaming_raises_on_non_200(monkeypatch):
    fake = _FakeConn(503, [b"upstream sad\n"])
    monkeypatch.setattr(
        openclaw_chat,
        "_connection_for",
        lambda host, port, timeout_s, scheme="http": fake,
    )
    with pytest.raises(RuntimeError, match="503"):
        openclaw_chat.post_chat_completion_streaming(
            gateway_host="127.0.0.1",
            gateway_port=1,
            token=None,
            user_message="x",
            system_prompt=None,
            history=[],
            model="openclaw",
            timeout_ms=1000,
            progress=lambda _m: None,
        )


def test_openclaw_streaming_raises_on_empty_content(monkeypatch):
    monkeypatch.setattr(openclaw_chat.time, "monotonic", lambda: 0.0)
    fake = _FakeConn(200, [b"data: [DONE]\n"])
    monkeypatch.setattr(
        openclaw_chat,
        "_connection_for",
        lambda host, port, timeout_s, scheme="http": fake,
    )
    with pytest.raises(RuntimeError, match="empty"):
        openclaw_chat.post_chat_completion_streaming(
            gateway_host="127.0.0.1",
            gateway_port=1,
            token=None,
            user_message="x",
            system_prompt=None,
            history=[],
            model="openclaw",
            timeout_ms=1000,
            progress=lambda _m: None,
        )


def test_openclaw_streaming_emits_progress_with_throttle(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr(openclaw_chat.time, "monotonic", lambda: fake_now[0])

    deltas = ["alpha", " beta", " gamma", " delta", " epsilon"]

    def advance(idx: int) -> None:
        if idx and idx % 2 == 0:
            fake_now[0] += 4.0

    fake = _FakeConn(200, _sse(*deltas), between=advance)

    monkeypatch.setattr(
        openclaw_chat,
        "_connection_for",
        lambda host, port, timeout_s, scheme="http": fake,
    )

    notes: list[str] = []
    text = openclaw_chat.post_chat_completion_streaming(
        gateway_host="127.0.0.1",
        gateway_port=1,
        token=None,
        user_message="x",
        system_prompt=None,
        history=[],
        model="openclaw",
        timeout_ms=1000,
        progress=notes.append,
        progress_interval_s=3.0,
    )
    assert text == "alpha beta gamma delta epsilon"
    # We should have at least one progress note, and char counts must be
    # non-decreasing across notes.
    assert notes
    counts = []
    for n in notes:
        # format: "streaming: N chars · …<tail>"
        head = n.split("chars")[0].rsplit(":", 1)[-1].strip()
        counts.append(int(head))
    assert counts == sorted(counts)
    assert counts[-1] <= len(text)
