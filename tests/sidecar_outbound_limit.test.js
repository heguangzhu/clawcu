"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  DEFAULT_RPM,
  WINDOW_MS,
  DEFAULT_SWEEP_INTERVAL_MS,
  readRpm,
  readSweepIntervalMs,
  keyFor,
  createOutboundLimiter,
  createSweepTimer,
} = require("../src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/outbound_limit.js");

test("readRpm returns default when env is empty or invalid", () => {
  assert.equal(readRpm({}), DEFAULT_RPM);
  assert.equal(readRpm({ A2A_OUTBOUND_RATE_LIMIT: "" }), DEFAULT_RPM);
  assert.equal(readRpm({ A2A_OUTBOUND_RATE_LIMIT: "abc" }), DEFAULT_RPM);
  assert.equal(readRpm({ A2A_OUTBOUND_RATE_LIMIT: "-5" }), DEFAULT_RPM);
  assert.equal(readRpm({ A2A_OUTBOUND_RATE_LIMIT: "3.7" }), DEFAULT_RPM);
});

test("readRpm parses valid positive integers", () => {
  assert.equal(readRpm({ A2A_OUTBOUND_RATE_LIMIT: "10" }), 10);
  assert.equal(readRpm({ A2A_OUTBOUND_RATE_LIMIT: "1000" }), 1000);
});

test("keyFor prefers thread_id over selfName", () => {
  assert.equal(keyFor({ threadId: "t-1", selfName: "writer" }), "thread:t-1");
  assert.equal(keyFor({ threadId: "", selfName: "writer" }), "self:writer");
  assert.equal(keyFor({ selfName: "writer" }), "self:writer");
  assert.equal(keyFor({}), "self:anon");
});

test("limiter allows up to rpm calls per window", () => {
  let now = 1000;
  const lim = createOutboundLimiter({ rpm: 3, nowFn: () => now });
  assert.equal(lim.check("k").allowed, true);
  assert.equal(lim.check("k").allowed, true);
  assert.equal(lim.check("k").allowed, true);
  const r = lim.check("k");
  assert.equal(r.allowed, false);
  assert.ok(r.retryAfterMs > 0 && r.retryAfterMs <= WINDOW_MS);
  assert.equal(r.limit, 3);
});

test("limiter prunes entries older than the window", () => {
  let now = 1000;
  const lim = createOutboundLimiter({ rpm: 2, nowFn: () => now });
  assert.equal(lim.check("k").allowed, true);
  assert.equal(lim.check("k").allowed, true);
  assert.equal(lim.check("k").allowed, false);
  now += WINDOW_MS + 1; // slide past the window
  assert.equal(lim.check("k").allowed, true);
});

test("limiter buckets are per-key (different thread = own quota)", () => {
  let now = 1000;
  const lim = createOutboundLimiter({ rpm: 1, nowFn: () => now });
  assert.equal(lim.check("thread:a").allowed, true);
  assert.equal(lim.check("thread:b").allowed, true);
  assert.equal(lim.check("thread:a").allowed, false);
  assert.equal(lim.check("thread:b").allowed, false);
});

test("limiter defaults rpm when constructed with no args", () => {
  const lim = createOutboundLimiter();
  assert.equal(lim.limit, DEFAULT_RPM);
});

test("limiter reset clears all buckets", () => {
  let now = 1000;
  const lim = createOutboundLimiter({ rpm: 1, nowFn: () => now });
  lim.check("k");
  assert.equal(lim.check("k").allowed, false);
  lim.reset();
  assert.equal(lim.check("k").allowed, true);
});

// -- P1-J: empty-bucket sweep (a2a-design-5.md) -----------------------------

test("limiter sweep drops empty buckets after window slides past", () => {
  let now = 1000;
  const lim = createOutboundLimiter({ rpm: 5, nowFn: () => now });
  lim.check("a");
  lim.check("b");
  lim.check("c");
  assert.equal(lim.size(), 3);
  now += WINDOW_MS + 1;
  lim.sweep();
  assert.equal(lim.size(), 0, "all three buckets past the window must be gone");
});

test("limiter sweep leaves active buckets alone", () => {
  let now = 1000;
  const lim = createOutboundLimiter({ rpm: 5, nowFn: () => now });
  lim.check("a");
  now += WINDOW_MS + 1;
  lim.check("b");
  lim.sweep();
  assert.equal(lim.size(), 1, "only 'b' (inside window) should remain");
});

// -- P2-L: sweep timer (a2a-design-6.md) ------------------------------------

test("readSweepIntervalMs returns default when env is empty or invalid", () => {
  assert.equal(readSweepIntervalMs({}), DEFAULT_SWEEP_INTERVAL_MS);
  assert.equal(readSweepIntervalMs({ A2A_OUTBOUND_SWEEP_INTERVAL_MS: "" }), DEFAULT_SWEEP_INTERVAL_MS);
  assert.equal(readSweepIntervalMs({ A2A_OUTBOUND_SWEEP_INTERVAL_MS: "abc" }), DEFAULT_SWEEP_INTERVAL_MS);
  assert.equal(readSweepIntervalMs({ A2A_OUTBOUND_SWEEP_INTERVAL_MS: "1.5" }), DEFAULT_SWEEP_INTERVAL_MS);
});

test("readSweepIntervalMs parses positive integers and clamps negative to 0", () => {
  assert.equal(readSweepIntervalMs({ A2A_OUTBOUND_SWEEP_INTERVAL_MS: "60000" }), 60000);
  assert.equal(readSweepIntervalMs({ A2A_OUTBOUND_SWEEP_INTERVAL_MS: "0" }), 0);
  assert.equal(readSweepIntervalMs({ A2A_OUTBOUND_SWEEP_INTERVAL_MS: "-30" }), 0);
});

test("createSweepTimer returns null when interval is 0 (opt-out)", () => {
  const lim = createOutboundLimiter({ rpm: 1 });
  const fakeScheduler = () => { throw new Error("scheduler must not be called when disabled"); };
  const h = createSweepTimer({ limiter: lim, intervalMs: 0, scheduler: fakeScheduler });
  assert.equal(h, null);
});

test("createSweepTimer wires scheduler and invokes limiter.sweep on tick", () => {
  let nowMs = 1000;
  const lim = createOutboundLimiter({ rpm: 5, nowFn: () => nowMs });
  lim.check("a");
  lim.check("b");
  assert.equal(lim.size(), 2);
  let tickFn = null;
  const fakeScheduler = (fn, ms) => {
    assert.equal(ms, 60000);
    tickFn = fn;
    return { unref() {} };
  };
  const h = createSweepTimer({ limiter: lim, intervalMs: 60000, scheduler: fakeScheduler });
  assert.ok(h);
  nowMs += WINDOW_MS + 1; // slide past window
  tickFn(); // simulate a scheduler tick
  assert.equal(lim.size(), 0);
});

// -- P2-N: sweep-failure log (a2a-design-7.md) ------------------------------

test("createSweepTimer logs a warning when sweep throws but keeps timer alive", () => {
  const thrown = new Error("boom from sweep");
  const fakeLimiter = {
    sweep() {
      throw thrown;
    },
  };
  let tickFn = null;
  const fakeScheduler = (fn, _ms) => {
    tickFn = fn;
    return { unref() {} };
  };
  const captured = [];
  const origWarn = console.warn;
  console.warn = (msg) => {
    captured.push(String(msg));
  };
  try {
    const h = createSweepTimer({
      limiter: fakeLimiter,
      intervalMs: 1000,
      scheduler: fakeScheduler,
    });
    assert.ok(h);
    // tickFn must not propagate the sweep error — the scheduler would
    // otherwise kill the interval in real setInterval wiring.
    tickFn();
  } finally {
    console.warn = origWarn;
  }
  assert.equal(captured.length, 1, "one warning on sweep failure");
  assert.match(captured[0], /outbound-sweep failed/);
  assert.match(captured[0], /boom from sweep/);
});
