"use strict";

// Per-peer sliding-window rate limiter for the OpenClaw A2A sidecar.
// Review-9 P2-A: the sidecar forwards /a2a/send to /v1/chat/completions,
// which triggers a provider call (LLM token spend). A misbehaving or
// compromised peer could drain budget / hit provider quota fast. This is
// a simple per-peer-name sliding-window counter — not a global defense.
//
// Design choices:
//
// - **Sliding window (not token bucket)**: we want "no more than N in the
//   last M seconds" semantics, and sliding window is dead-simple to read
//   in logs. Token bucket's "burst" concept isn't useful here — chat is
//   second-scale, a burst of 10 isn't meaningfully different from 10
//   spread over the window.
//
// - **Per-peer key**: the `from` field from /a2a/send. Mis-signed peers
//   can spoof this, but the trust model already assumes Bearer-authed
//   peers — this is quota, not auth.
//
// - **In-memory only**: sidecar is per-instance; a peer that hops between
//   instances isn't our concern. Persistence across restart would add
//   complexity for no clear gain.
//
// - **0 = disabled**: let operators opt-out via
//   A2A_RATE_LIMIT_PER_MINUTE=0. Disabled by default would make the
//   feature dead-code; enabled with a reasonable default (30/min) is
//   conservative enough that legitimate agent chat won't hit it.

function createRateLimiter({
  perMinute = 30,
  windowMs = 60 * 1000,
  nowFn = Date.now,
  maxPeers = 1024,
} = {}) {
  // peer name → array of timestamps (ms). Arrays kept small by trimming
  // on each check; maxPeers cap bounds memory if attackers rotate names.
  const hits = new Map();

  function allow(peer) {
    if (perMinute <= 0) return { ok: true, remaining: Infinity, resetMs: 0 };
    const now = nowFn();
    const windowStart = now - windowMs;
    let timestamps = hits.get(peer);
    if (!timestamps) {
      if (hits.size >= maxPeers) {
        // Evict the stalest peer to stay bounded. O(n) scan; maxPeers is
        // small enough (1024) that this is fine for sidecar traffic.
        let stalestKey = null;
        let stalestTs = Infinity;
        for (const [k, ts] of hits.entries()) {
          const last = ts[ts.length - 1] ?? 0;
          if (last < stalestTs) {
            stalestTs = last;
            stalestKey = k;
          }
        }
        if (stalestKey !== null) hits.delete(stalestKey);
      }
      timestamps = [];
      hits.set(peer, timestamps);
    }
    // Trim timestamps outside the window.
    while (timestamps.length && timestamps[0] < windowStart) {
      timestamps.shift();
    }
    if (timestamps.length >= perMinute) {
      const oldest = timestamps[0];
      const resetMs = Math.max(0, oldest + windowMs - now);
      return { ok: false, remaining: 0, resetMs };
    }
    timestamps.push(now);
    return { ok: true, remaining: perMinute - timestamps.length, resetMs: 0 };
  }

  function _peers() {
    return new Map(hits);
  }

  return { allow, _peers };
}

module.exports = { createRateLimiter };
