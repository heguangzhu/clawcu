"use strict";

// Node-test unit tests for the openclaw sidecar readiness primitives.
// Run with: node --test tests/sidecar_readiness.test.js
//
// Pytest ignores this file (filename pattern doesn't match test_*.py), but
// we keep it under tests/ so human readers find it alongside the python
// suites. The module under test lives at
// src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/readiness.js (review-8 P1-G).

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const readiness = require(
  path.resolve(
    __dirname,
    "..",
    "src",
    "clawcu",
    "a2a",
    "sidecar_plugin",
    "openclaw",
    "sidecar",
    "readiness.js",
  ),
);

// -- looksLikeGatewayDown ----------------------------------------------------

test("looksLikeGatewayDown: ECONNREFUSED is gateway-down", () => {
  assert.equal(readiness.looksLikeGatewayDown("connect ECONNREFUSED 127.0.0.1:18789"), true);
});

test("looksLikeGatewayDown: ETIMEDOUT is gateway-down", () => {
  assert.equal(readiness.looksLikeGatewayDown(new Error("request timeout ETIMEDOUT")), true);
});

test("looksLikeGatewayDown: /v1/chat/completions 5xx is gateway-down", () => {
  assert.equal(
    readiness.looksLikeGatewayDown("gateway /v1/chat/completions 503"),
    true,
  );
});

test("looksLikeGatewayDown: non-json body is gateway-down", () => {
  assert.equal(readiness.looksLikeGatewayDown("gateway returned non-json body: foo"), true);
});

test("looksLikeGatewayDown: 4xx is NOT gateway-down (auth/client errors)", () => {
  assert.equal(
    readiness.looksLikeGatewayDown("gateway /v1/chat/completions 401"),
    false,
  );
});

test("looksLikeGatewayDown: bland message is NOT gateway-down", () => {
  assert.equal(readiness.looksLikeGatewayDown("something went wrong"), false);
});

// -- createReadiness cache behavior -----------------------------------------

test("createReadiness: cache hit short-circuits probe", async () => {
  let clock = 1000;
  const nowFn = () => clock;
  const { waitForGatewayReady, _readyUntil } = readiness.createReadiness({ nowFn });

  let probeCalls = 0;
  const fakeProbe = async () => {
    probeCalls++;
    return true;
  };

  const ok1 = await waitForGatewayReady({
    host: "x",
    port: 1,
    deadlineMs: 100,
    probe: fakeProbe,
  });
  assert.equal(ok1, true);
  assert.equal(probeCalls, 1, "first call must probe");
  assert.ok(_readyUntil() > clock, "cache end must be in the future");

  clock += 1000; // still inside TTL (5 min default)
  const ok2 = await waitForGatewayReady({
    host: "x",
    port: 1,
    deadlineMs: 100,
    probe: fakeProbe,
  });
  assert.equal(ok2, true);
  assert.equal(probeCalls, 1, "cache hit must skip the second probe");
});

test("createReadiness: invalidate drops cache, next call re-probes", async () => {
  let clock = 0;
  const nowFn = () => clock;
  const { waitForGatewayReady, invalidateGatewayReady } = readiness.createReadiness({ nowFn });

  let probeCalls = 0;
  const fakeProbe = async () => {
    probeCalls++;
    return true;
  };

  await waitForGatewayReady({ host: "x", port: 1, deadlineMs: 100, probe: fakeProbe });
  assert.equal(probeCalls, 1);

  invalidateGatewayReady();
  await waitForGatewayReady({ host: "x", port: 1, deadlineMs: 100, probe: fakeProbe });
  assert.equal(probeCalls, 2, "invalidate must force a re-probe on the next call");
});

test("createReadiness: probe timeout returns false without hanging", async () => {
  let clock = 0;
  const nowFn = () => clock;
  const { waitForGatewayReady } = readiness.createReadiness({
    nowFn,
    sleepFn: async (ms) => {
      clock += ms;
    },
  });

  const alwaysFail = async () => false;
  const ok = await waitForGatewayReady({
    host: "x",
    port: 1,
    deadlineMs: 50,
    pollIntervalMs: 20,
    probe: alwaysFail,
  });
  assert.equal(ok, false, "must give up once deadline elapses");
});

test("createReadiness: passes path through to probe", async () => {
  let clock = 0;
  const nowFn = () => clock;
  const { waitForGatewayReady } = readiness.createReadiness({ nowFn });

  let seenPath = null;
  const pathSpy = async ({ path }) => {
    seenPath = path;
    return true;
  };

  await waitForGatewayReady({
    host: "x",
    port: 1,
    path: "/custom-health",
    deadlineMs: 100,
    probe: pathSpy,
  });
  assert.equal(seenPath, "/custom-health");
});
