"use strict";

// Node-test unit tests for the openclaw sidecar per-peer rate limiter.
// Run with: node --test tests/sidecar_ratelimit.test.js
//
// Pytest ignores this file (filename pattern doesn't match test_*.py). The
// module under test lives at
// src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/ratelimit.js (review-9 P2-A).

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createRateLimiter } = require(
  path.resolve(
    __dirname,
    "..",
    "src",
    "clawcu",
    "a2a",
    "sidecar_plugin",
    "openclaw",
    "sidecar",
    "ratelimit.js",
  ),
);

test("allows requests under the per-minute cap", () => {
  let clock = 0;
  const limiter = createRateLimiter({ perMinute: 3, nowFn: () => clock });
  for (let i = 0; i < 3; i++) {
    const r = limiter.allow("peer-a");
    assert.equal(r.ok, true, `hit ${i + 1} must be allowed`);
  }
});

test("blocks once the per-minute cap is exceeded, and reports resetMs", () => {
  let clock = 0;
  const limiter = createRateLimiter({ perMinute: 2, nowFn: () => clock });
  assert.equal(limiter.allow("peer-a").ok, true);
  clock += 100;
  assert.equal(limiter.allow("peer-a").ok, true);
  clock += 100;
  const blocked = limiter.allow("peer-a");
  assert.equal(blocked.ok, false);
  assert.equal(blocked.remaining, 0);
  // Oldest hit was at clock=0 with a 60s window; we're at clock=200, so the
  // window frees at clock=60_000 → resetMs ≈ 59800.
  assert.ok(blocked.resetMs > 0, "blocked responses must advertise a non-zero reset");
  assert.ok(blocked.resetMs <= 60_000, "reset bounded by the window size");
});

test("sliding window: old hits drop out, peer is allowed again", () => {
  let clock = 0;
  const limiter = createRateLimiter({ perMinute: 2, nowFn: () => clock });
  assert.equal(limiter.allow("peer-a").ok, true);
  assert.equal(limiter.allow("peer-a").ok, true);
  assert.equal(limiter.allow("peer-a").ok, false);
  // Advance past the 60s window — both prior hits should expire.
  clock += 61_000;
  const r = limiter.allow("peer-a");
  assert.equal(r.ok, true, "after window, peer should be allowed again");
});

test("per-peer isolation: peer B is not affected by peer A's flood", () => {
  let clock = 0;
  const limiter = createRateLimiter({ perMinute: 2, nowFn: () => clock });
  assert.equal(limiter.allow("peer-a").ok, true);
  assert.equal(limiter.allow("peer-a").ok, true);
  assert.equal(limiter.allow("peer-a").ok, false, "peer-a exhausted");
  // peer-b has its own window.
  assert.equal(limiter.allow("peer-b").ok, true);
  assert.equal(limiter.allow("peer-b").ok, true);
});

test("perMinute=0 disables the limiter entirely", () => {
  const limiter = createRateLimiter({ perMinute: 0 });
  for (let i = 0; i < 100; i++) {
    const r = limiter.allow("peer-a");
    assert.equal(r.ok, true);
    assert.equal(r.remaining, Infinity);
  }
});

test("maxPeers eviction: stalest peer is evicted when the cap is reached", () => {
  let clock = 0;
  const limiter = createRateLimiter({
    perMinute: 5,
    nowFn: () => clock,
    maxPeers: 2,
  });
  limiter.allow("peer-a"); // clock=0
  clock += 1000;
  limiter.allow("peer-b"); // clock=1000
  assert.equal(limiter._peers().size, 2);
  clock += 1000;
  limiter.allow("peer-c"); // should evict peer-a (stalest)
  const peers = limiter._peers();
  assert.equal(peers.size, 2);
  assert.equal(peers.has("peer-a"), false, "stalest peer evicted");
  assert.equal(peers.has("peer-b"), true);
  assert.equal(peers.has("peer-c"), true);
});

test("remaining counter decrements with each allowed hit", () => {
  let clock = 0;
  const limiter = createRateLimiter({ perMinute: 3, nowFn: () => clock });
  assert.equal(limiter.allow("peer-a").remaining, 2);
  assert.equal(limiter.allow("peer-a").remaining, 1);
  assert.equal(limiter.allow("peer-a").remaining, 0);
});
