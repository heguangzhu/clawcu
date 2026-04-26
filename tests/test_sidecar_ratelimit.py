"""pytest port of tests/sidecar_ratelimit.test.js."""
from __future__ import annotations

import math
import os
import sys

_COMMON_PARENT = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
    )
)
if _COMMON_PARENT not in sys.path:
    sys.path.insert(0, _COMMON_PARENT)

from _common.ratelimit import create_rate_limiter  # noqa: E402


class _Clock:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value


def test_allows_under_per_minute_cap():
    clock = _Clock()
    limiter = create_rate_limiter(per_minute=3, now_fn=clock)
    for i in range(3):
        r = limiter.allow("peer-a")
        assert r.ok is True, f"hit {i + 1} must be allowed"


def test_blocks_after_cap_and_reports_reset_ms():
    clock = _Clock()
    limiter = create_rate_limiter(per_minute=2, now_fn=clock)
    assert limiter.allow("peer-a").ok is True
    clock.value += 100
    assert limiter.allow("peer-a").ok is True
    clock.value += 100
    blocked = limiter.allow("peer-a")
    assert blocked.ok is False
    assert blocked.remaining == 0
    assert 0 < blocked.reset_ms <= 60_000


def test_sliding_window_old_hits_expire():
    clock = _Clock()
    limiter = create_rate_limiter(per_minute=2, now_fn=clock)
    assert limiter.allow("peer-a").ok is True
    assert limiter.allow("peer-a").ok is True
    assert limiter.allow("peer-a").ok is False
    clock.value += 61_000
    assert limiter.allow("peer-a").ok is True


def test_per_peer_isolation():
    clock = _Clock()
    limiter = create_rate_limiter(per_minute=2, now_fn=clock)
    assert limiter.allow("peer-a").ok is True
    assert limiter.allow("peer-a").ok is True
    assert limiter.allow("peer-a").ok is False
    assert limiter.allow("peer-b").ok is True
    assert limiter.allow("peer-b").ok is True


def test_per_minute_zero_disables():
    limiter = create_rate_limiter(per_minute=0)
    for _ in range(100):
        r = limiter.allow("peer-a")
        assert r.ok is True
        assert r.remaining == math.inf


def test_max_peers_evicts_stalest():
    clock = _Clock()
    limiter = create_rate_limiter(per_minute=5, now_fn=clock, max_peers=2)
    limiter.allow("peer-a")
    clock.value += 1000
    limiter.allow("peer-b")
    assert len(limiter._peers()) == 2
    clock.value += 1000
    limiter.allow("peer-c")
    peers = limiter._peers()
    assert len(peers) == 2
    assert "peer-a" not in peers, "stalest peer evicted"
    assert "peer-b" in peers
    assert "peer-c" in peers


def test_remaining_counter_decrements():
    clock = _Clock()
    limiter = create_rate_limiter(per_minute=3, now_fn=clock)
    assert limiter.allow("peer-a").remaining == 2
    assert limiter.allow("peer-a").remaining == 1
    assert limiter.allow("peer-a").remaining == 0
