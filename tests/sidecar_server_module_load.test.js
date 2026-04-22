"use strict";

// a2a-design-7.md §P1-N: require("server.js") from a test harness must
// not start a real setInterval. Before iter 7, server.js called
// createOutboundSweepTimer at module scope; iter 7 moved the call into
// main() so tests that pull in exports (lookupPeer, forwardToPeer, …)
// no longer leak a timer handle per-require.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

test("require(server.js) does not register a setInterval at module scope", () => {
  // Ensure a clean require-cache entry so the module body runs now.
  const serverPath = path.resolve(
    __dirname,
    "../src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/server.js",
  );
  delete require.cache[require.resolve(serverPath)];

  const origSetInterval = global.setInterval;
  const intervals = [];
  global.setInterval = (fn, ms) => {
    intervals.push({ fn, ms });
    // Return a handle shaped like a Timeout so any module-scope
    // `.unref()` calls don't crash the load. We never fire the fn.
    return { unref() {}, ref() {} };
  };

  try {
    require(serverPath);
  } finally {
    global.setInterval = origSetInterval;
  }

  assert.equal(
    intervals.length,
    0,
    "module load must not register any setInterval — the sweep timer belongs inside main()",
  );
});
