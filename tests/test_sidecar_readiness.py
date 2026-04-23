"""pytest port of tests/sidecar_readiness.test.js."""
from __future__ import annotations

import os
import sys

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
# readiness.py imports ``_common.readiness`` (the shared TTL-cache primitive),
# so the tests need the sidecar_plugin/ dir on sys.path the same way server.py
# arranges it at runtime.
_SIDECAR_PLUGIN = os.path.abspath(os.path.join(_SIDECAR, "..", ".."))
for _p in (_SIDECAR, _SIDECAR_PLUGIN):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import readiness  # noqa: E402


# -- looks_like_gateway_down -------------------------------------------------


def test_looks_like_gateway_down_econnrefused():
    assert readiness.looks_like_gateway_down("connect ECONNREFUSED 127.0.0.1:18789") is True


def test_looks_like_gateway_down_etimedout():
    assert readiness.looks_like_gateway_down(RuntimeError("request timeout ETIMEDOUT")) is True


def test_looks_like_gateway_down_5xx():
    assert readiness.looks_like_gateway_down("gateway /v1/chat/completions 503") is True


def test_looks_like_gateway_down_non_json_body():
    assert readiness.looks_like_gateway_down("gateway returned non-json body: foo") is True


def test_looks_like_gateway_down_4xx_is_not():
    assert readiness.looks_like_gateway_down("gateway /v1/chat/completions 401") is False


def test_looks_like_gateway_down_bland_message_is_not():
    assert readiness.looks_like_gateway_down("something went wrong") is False


# -- createReadiness cache behavior ------------------------------------------


class _Clock:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value


def test_create_readiness_cache_hits_short_circuit_probe():
    clock = _Clock(1000)
    r = readiness.create_readiness(now_fn=clock)
    calls = {"n": 0}

    def fake_probe(**_kwargs):
        calls["n"] += 1
        return True

    ok1 = r.wait_for_gateway_ready(host="x", port=1, deadline_ms=100, probe=fake_probe)
    assert ok1 is True
    assert calls["n"] == 1
    assert r._ready_until_value() > clock.value

    clock.value += 1000
    ok2 = r.wait_for_gateway_ready(host="x", port=1, deadline_ms=100, probe=fake_probe)
    assert ok2 is True
    assert calls["n"] == 1, "cache hit must skip the second probe"


def test_invalidate_drops_cache_and_forces_reprobe():
    clock = _Clock(0)
    r = readiness.create_readiness(now_fn=clock)
    calls = {"n": 0}

    def fake_probe(**_kwargs):
        calls["n"] += 1
        return True

    r.wait_for_gateway_ready(host="x", port=1, deadline_ms=100, probe=fake_probe)
    assert calls["n"] == 1
    r.invalidate_gateway_ready()
    r.wait_for_gateway_ready(host="x", port=1, deadline_ms=100, probe=fake_probe)
    assert calls["n"] == 2


def test_probe_timeout_returns_false_without_hanging():
    clock = _Clock(0)

    def fake_sleep(ms: int) -> None:
        clock.value += ms

    r = readiness.create_readiness(now_fn=clock, sleep_fn=fake_sleep)

    def always_fail(**_kwargs):
        return False

    ok = r.wait_for_gateway_ready(
        host="x", port=1, deadline_ms=50, poll_interval_ms=20, probe=always_fail
    )
    assert ok is False


def test_passes_path_through_to_probe():
    clock = _Clock(0)
    r = readiness.create_readiness(now_fn=clock)
    seen = {"path": None}

    def spy(**kwargs):
        seen["path"] = kwargs.get("path")
        return True

    r.wait_for_gateway_ready(
        host="x", port=1, path="/custom-health", deadline_ms=100, probe=spy
    )
    assert seen["path"] == "/custom-health"
