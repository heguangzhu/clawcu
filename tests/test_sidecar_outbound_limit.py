"""pytest port of tests/sidecar_outbound_limit.test.js."""
from __future__ import annotations

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

from outbound_limit import (  # noqa: E402
    DEFAULT_RPM,
    DEFAULT_SWEEP_INTERVAL_MS,
    WINDOW_MS,
    create_outbound_limiter,
    key_for,
    read_rpm,
    read_sweep_interval_ms,
)


class _Clock:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value


# ---- read_rpm ----


def test_read_rpm_default_when_missing_or_invalid():
    assert read_rpm({}) == DEFAULT_RPM
    assert read_rpm({"A2A_OUTBOUND_RATE_LIMIT": ""}) == DEFAULT_RPM
    assert read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "abc"}) == DEFAULT_RPM
    assert read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "-5"}) == DEFAULT_RPM
    assert read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "3.7"}) == DEFAULT_RPM


def test_read_rpm_parses_positive_integers():
    assert read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "10"}) == 10
    assert read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "1000"}) == 1000


# ---- key_for ----


def test_key_for_prefers_thread_id():
    assert key_for(thread_id="t-1", self_name="writer") == "thread:t-1"
    assert key_for(thread_id="", self_name="writer") == "self:writer"
    assert key_for(self_name="writer") == "self:writer"
    assert key_for() == "self:anon"


# ---- limiter basics ----


def test_limiter_allows_up_to_rpm_calls_per_window():
    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=3, now_fn=clock)
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is True
    r = lim.check("k")
    assert r.allowed is False
    assert 0 < r.retry_after_ms <= WINDOW_MS
    assert r.limit == 3


def test_limiter_prunes_entries_older_than_window():
    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=2, now_fn=clock)
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is False
    clock.value += WINDOW_MS + 1
    assert lim.check("k").allowed is True


def test_limiter_buckets_are_per_key():
    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=1, now_fn=clock)
    assert lim.check("thread:a").allowed is True
    assert lim.check("thread:b").allowed is True
    assert lim.check("thread:a").allowed is False
    assert lim.check("thread:b").allowed is False


def test_limiter_default_rpm_when_no_args():
    lim = create_outbound_limiter()
    assert lim.limit == DEFAULT_RPM


def test_limiter_reset_clears_all_buckets():
    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=1, now_fn=clock)
    lim.check("k")
    assert lim.check("k").allowed is False
    lim.reset()
    assert lim.check("k").allowed is True


# ---- sweep ----


def test_limiter_sweep_drops_empty_buckets():
    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=5, now_fn=clock)
    lim.check("a")
    lim.check("b")
    lim.check("c")
    assert lim.size() == 3
    clock.value += WINDOW_MS + 1
    lim.sweep()
    assert lim.size() == 0


def test_limiter_sweep_leaves_active_buckets_alone():
    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=5, now_fn=clock)
    lim.check("a")
    clock.value += WINDOW_MS + 1
    lim.check("b")
    lim.sweep()
    assert lim.size() == 1


# ---- read_sweep_interval_ms ----


def test_read_sweep_interval_default():
    assert read_sweep_interval_ms({}) == DEFAULT_SWEEP_INTERVAL_MS
    assert read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": ""}) == DEFAULT_SWEEP_INTERVAL_MS
    assert read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "abc"}) == DEFAULT_SWEEP_INTERVAL_MS
    assert read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "1.5"}) == DEFAULT_SWEEP_INTERVAL_MS


def test_read_sweep_interval_parses_and_clamps():
    assert read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "60000"}) == 60000
    assert read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "0"}) == 0
    assert read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "-30"}) == 0


# ---- sweep timer ----


def test_sweep_timer_returns_none_when_interval_zero():
    from outbound_limit import create_sweep_timer

    lim = create_outbound_limiter(rpm=1)
    h = create_sweep_timer(limiter=lim, interval_ms=0)
    assert h is None


def test_sweep_timer_invokes_sweep_periodically():
    """Uses a very short interval to confirm the daemon thread fires at least once."""
    import time
    from outbound_limit import SweepTimer

    clock = _Clock(1000)
    lim = create_outbound_limiter(rpm=5, now_fn=clock)
    lim.check("a")
    lim.check("b")
    assert lim.size() == 2
    clock.value += WINDOW_MS + 1
    timer = SweepTimer(limiter=lim, interval_ms=50)
    timer.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lim.size() > 0:
            time.sleep(0.02)
    finally:
        timer.stop()
    assert lim.size() == 0


def test_sweep_timer_swallows_exceptions_and_logs():
    """When sweep throws, the timer keeps running and logs a warning."""
    import time
    from outbound_limit import SweepTimer

    class ExplodingLimiter:
        def __init__(self):
            self.calls = 0

        def sweep(self):
            self.calls += 1
            raise RuntimeError("boom from sweep")

    class CapturingLogger:
        def __init__(self):
            self.warnings = []

        def warn(self, *args):
            self.warnings.append(" ".join(str(a) for a in args))

    limiter = ExplodingLimiter()
    log = CapturingLogger()
    timer = SweepTimer(limiter=limiter, interval_ms=50, logger=log)
    timer.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and limiter.calls < 1:
            time.sleep(0.02)
    finally:
        timer.stop()

    assert limiter.calls >= 1, "sweep was called at least once"
    assert any("outbound-sweep failed" in w for w in log.warnings)
    assert any("boom from sweep" in w for w in log.warnings)
