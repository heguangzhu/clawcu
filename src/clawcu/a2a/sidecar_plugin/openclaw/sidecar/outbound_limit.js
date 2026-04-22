"use strict";

// Self-origin outbound rate limit shared by /a2a/outbound and /mcp
// tool-call handlers (a2a-design-4.md §P1-B). Hop budget caps depth
// (A→B→A→B runaway); this caps breadth (one LLM turn fires 200 parallel
// a2a_call_peer calls and nukes the provider quota).
//
// Key: thread_id when present, else the caller's own registered name
// (`self:<name>`). Limit: N calls / rolling 60s / key. Default 60/min,
// tunable via A2A_OUTBOUND_RATE_LIMIT env var.

const DEFAULT_RPM = 60;
const WINDOW_MS = 60_000;

function readRpm(env) {
  const raw = (env || process.env).A2A_OUTBOUND_RATE_LIMIT;
  if (raw === undefined || raw === null || String(raw).trim() === "") return DEFAULT_RPM;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0 || Math.floor(n) !== n) return DEFAULT_RPM;
  return n;
}

function keyFor({ threadId, selfName }) {
  if (typeof threadId === "string" && threadId) return `thread:${threadId}`;
  return `self:${selfName || "anon"}`;
}

function createOutboundLimiter({ rpm, nowFn = Date.now } = {}) {
  const limit = Number.isFinite(rpm) && rpm > 0 ? rpm : DEFAULT_RPM;
  const hits = new Map();

  function check(key) {
    const now = nowFn();
    const cutoff = now - WINDOW_MS;
    const arr = hits.get(key) || [];
    // prune old entries
    while (arr.length && arr[0] <= cutoff) arr.shift();
    if (arr.length >= limit) {
      const retryAfterMs = arr[0] + WINDOW_MS - now;
      hits.set(key, arr);
      return { allowed: false, retryAfterMs: Math.max(0, retryAfterMs), limit };
    }
    arr.push(now);
    hits.set(key, arr);
    return { allowed: true, count: arr.length, limit };
  }

  // P1-J (a2a-design-5.md): sweep buckets whose deques have emptied out
  // past the window. Called opportunistically — cheap when there are few
  // keys, fine to skip under load.
  function sweep() {
    const now = nowFn();
    const cutoff = now - WINDOW_MS;
    for (const [k, arr] of hits) {
      while (arr.length && arr[0] <= cutoff) arr.shift();
      if (arr.length === 0) hits.delete(k);
    }
  }

  function size() {
    return hits.size;
  }

  function reset() {
    hits.clear();
  }

  return { check, reset, sweep, size, limit };
}

const DEFAULT_SWEEP_INTERVAL_MS = 300_000;

function readSweepIntervalMs(env) {
  const raw = (env || process.env).A2A_OUTBOUND_SWEEP_INTERVAL_MS;
  if (raw === undefined || raw === null || String(raw).trim() === "") return DEFAULT_SWEEP_INTERVAL_MS;
  const n = Number(raw);
  if (!Number.isFinite(n) || Math.floor(n) !== n) return DEFAULT_SWEEP_INTERVAL_MS;
  return Math.max(0, n);
}

// a2a-design-6.md §P2-L: wire a periodic sweep so long-running sidecars
// with many distinct thread_ids over days don't accumulate empty buckets.
// `scheduler` is injectable for tests; defaults to setInterval. Returns
// the handle (or null when intervalMs <= 0). The caller should .unref()
// the handle so the timer doesn't block graceful shutdown.
function createSweepTimer({ limiter, intervalMs, scheduler = setInterval }) {
  if (!limiter || typeof limiter.sweep !== "function") return null;
  if (!Number.isFinite(intervalMs) || intervalMs <= 0) return null;
  const handle = scheduler(() => {
    try {
      limiter.sweep();
    } catch (err) {
      // a2a-design-7.md §P2-N: sweep is opportunistic cleanup, never
      // load-bearing — still swallow, but leave a breadcrumb so an
      // operator grepping logs can see something went wrong.
      try {
        console.warn(
          `[sidecar] outbound-sweep failed: ${err && err.message ? err.message : err}`,
        );
      } catch (_logErr) {
        // console itself faked/broken — keep the timer alive regardless.
      }
    }
  }, intervalMs);
  if (handle && typeof handle.unref === "function") handle.unref();
  return handle;
}

module.exports = {
  DEFAULT_RPM,
  WINDOW_MS,
  DEFAULT_SWEEP_INTERVAL_MS,
  readRpm,
  readSweepIntervalMs,
  keyFor,
  createOutboundLimiter,
  createSweepTimer,
};
