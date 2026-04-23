"""Shared gateway-readiness cache.

Both sidecars lazily probe their upstream gateway (Hermes ``/health``,
OpenClaw ``/healthz``) to avoid 502-ing an inbound ``/a2a/send`` that
arrives before the gateway has finished booting. A successful probe is
cached for a TTL so subsequent messages don't re-probe; a mid-flight
failure signal invalidates the cache so the *next* message re-probes
instead of pushing more load into a dead gateway.

This module owns the TTL + invalidate state machine as a pure storage
class — callers provide the clock value on each method invocation. The
HTTP probe stays with each sidecar (different stdlib HTTP helpers,
different health paths, different units for ``now``).
"""

from __future__ import annotations


class ReadinessCache:
    """TTL cache of a positive readiness observation.

    The caller provides ``now`` on each call, so the cache is unit-
    agnostic: Hermes passes seconds-since-epoch, OpenClaw passes
    milliseconds, tests pass whatever their fake clock returns. State
    is a single expiry deadline in the caller's unit.
    """

    __slots__ = ("_expires_at",)

    def __init__(self) -> None:
        self._expires_at: float = 0.0

    def is_fresh(self, now: float) -> bool:
        return now < self._expires_at

    def mark_ready(self, now: float, ttl: float) -> None:
        """Grant ``ttl`` time units of freshness from ``now``."""
        self._expires_at = now + ttl

    def mark_ready_until(self, when: float) -> None:
        """Set the expiry deadline directly. Intended for tests that want to
        pre-seed a far-future deadline without depending on a clock."""
        self._expires_at = when

    def invalidate(self) -> None:
        self._expires_at = 0.0

    @property
    def expires_at(self) -> float:
        return self._expires_at


__all__ = ["ReadinessCache"]
