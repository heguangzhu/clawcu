"use strict";

// Gateway readiness primitives for the OpenClaw A2A sidecar.
//
// Carved out of server.js (review-8 P1-G) so the cache + invalidation logic
// can be unit-tested without standing up the full HTTP server. The module is
// stateful (one in-process cache timestamp), so call createReadiness() when
// you want an isolated state (e.g. in tests) and module-level singletons for
// the real sidecar.

const http = require("node:http");

// The "gateway is reachable right now" observation is cheap to reuse: one
// successful probe keeps the sidecar out of probe-storm territory while the
// gateway is healthy. 5 minutes matches the hermes sidecar.
const READY_TTL_MS = 5 * 60 * 1000;

// Errors that suggest the gateway process died mid-flight (as opposed to
// misconfigured auth or a bad model name). Used to decide whether to drop
// the ready cache immediately on upstream failure, so a dead gateway is
// detected on the next call rather than after the TTL. See review-4 P1-A.
//
// Exported so tests can lock in the contract — and so future heuristics
// land here instead of duplicated string regex at the call site.
const GATEWAY_DOWN_PATTERNS = [
  /ECONNREFUSED|ECONNRESET|ETIMEDOUT|socket hang up/i,
  /gateway \/v1\/chat\/completions 5\d\d/,
  /gateway returned non-json/,
];

function looksLikeGatewayDown(errOrMessage) {
  const msg =
    typeof errOrMessage === "string"
      ? errOrMessage
      : (errOrMessage && errOrMessage.message) || "";
  for (const re of GATEWAY_DOWN_PATTERNS) {
    if (re.test(msg)) return true;
  }
  return false;
}

// probeGatewayReady: single GET against the gateway's readiness path.
// 200-range or 3xx redirect (<400) counts as ready; anything else (socket
// error, 4xx, 5xx, timeout) counts as not-ready.
function probeGatewayReady({
  host,
  port,
  path = "/healthz",
  timeoutMs = 2000,
  httpModule = http,
}) {
  return new Promise((resolve) => {
    const req = httpModule.request(
      { method: "GET", host, port, path },
      (res) => {
        res.on("data", () => {});
        res.on("end", () =>
          resolve((res.statusCode ?? 0) >= 200 && (res.statusCode ?? 0) < 400)
        );
      },
    );
    req.on("error", () => resolve(false));
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(false);
    });
    req.end();
  });
}

// createReadiness: isolated cache state. server.js uses a module-level
// singleton (see bottom of this file); tests use fresh instances so they
// don't step on each other.
function createReadiness({
  ttlMs = READY_TTL_MS,
  nowFn = Date.now,
  sleepFn,
} = {}) {
  let readyUntil = 0;
  const sleep =
    sleepFn ||
    ((ms) => new Promise((r) => setTimeout(r, ms)));

  async function waitForGatewayReady({
    host,
    port,
    path = "/healthz",
    deadlineMs,
    probeTimeoutMs = 2000,
    pollIntervalMs = 500,
    probe = probeGatewayReady,
  }) {
    const now = nowFn();
    if (now < readyUntil) return true;
    const end = now + deadlineMs;
    while (nowFn() < end) {
      const ok = await probe({ host, port, path, timeoutMs: probeTimeoutMs });
      if (ok) {
        readyUntil = nowFn() + ttlMs;
        return true;
      }
      await sleep(pollIntervalMs);
    }
    return false;
  }

  function invalidateGatewayReady() {
    readyUntil = 0;
  }

  function _readyUntil() {
    return readyUntil;
  }

  return {
    waitForGatewayReady,
    invalidateGatewayReady,
    _readyUntil,
  };
}

// Module-level singleton for server.js. Tests should prefer
// createReadiness() to avoid cross-test state bleed.
const _default = createReadiness();

module.exports = {
  READY_TTL_MS,
  GATEWAY_DOWN_PATTERNS,
  looksLikeGatewayDown,
  probeGatewayReady,
  createReadiness,
  waitForGatewayReady: _default.waitForGatewayReady,
  invalidateGatewayReady: _default.invalidateGatewayReady,
};
