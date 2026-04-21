"use strict";

// Node-test for the openclaw sidecar log-tee setup.
// Run with: node --test tests/sidecar_logsink.test.js
//
// Pytest ignores this file (filename doesn't match test_*.py). Review-10
// P2-C added tests/sidecar_logsink.test.js alongside readiness/ratelimit.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { setupFileLog } = require(
  path.resolve(
    __dirname,
    "..",
    "src",
    "clawcu",
    "a2a",
    "sidecar_plugin",
    "openclaw",
    "sidecar",
    "logsink.js",
  ),
);

async function withTempDir(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "logsink-"));
  // Save originals so we can restore console.log/error between tests —
  // setupFileLog mutates them globally.
  const origLog = console.log;
  const origErr = console.error;
  try {
    return await fn(dir);
  } finally {
    console.log = origLog;
    console.error = origErr;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function endStream(stream) {
  return new Promise((resolve, reject) => {
    stream.once("finish", resolve);
    stream.once("error", reject);
    stream.end();
  });
}

test("setupFileLog: no dir → no-op, returns not installed", () => {
  const origLog = console.log;
  try {
    const result = setupFileLog("");
    assert.equal(result.installed, false);
    assert.equal(console.log, origLog, "console.log must be untouched");
  } finally {
    console.log = origLog;
  }
});

test("setupFileLog: writes INFO and ERROR lines to a2a-sidecar.log", async () => {
  await withTempDir(async (dir) => {
    const result = setupFileLog(dir);
    assert.equal(result.installed, true);

    console.log("hello", { peer: "a" });
    console.error(new Error("boom"));

    // createWriteStream is async; awaiting "finish" guarantees the
    // backing fd was opened, drained, and closed before we read.
    await endStream(result.stream);

    const logPath = path.join(dir, "a2a-sidecar.log");
    const body = fs.readFileSync(logPath, "utf8");
    assert.match(body, /INFO hello \{"peer":"a"\}/, "INFO line must be teed");
    assert.match(body, /ERROR Error: boom/, "ERROR line must include stack");
    // ISO timestamp at the start of each line.
    const first = body.split("\n")[0];
    assert.match(first, /^\d{4}-\d{2}-\d{2}T/);
  });
});

test("setupFileLog: mkdir creates a missing parent directory", async () => {
  await withTempDir(async (dir) => {
    const nested = path.join(dir, "a", "b", "c");
    const result = setupFileLog(nested);
    assert.equal(result.installed, true);
    console.log("nested");
    await endStream(result.stream);
    assert.ok(fs.existsSync(path.join(nested, "a2a-sidecar.log")));
  });
});

test("setupFileLog: stderr tee still happens (we don't redirect, we tee)", async () => {
  await withTempDir(async (dir) => {
    // Capture process.stdout.write so we can verify the original console.log
    // path is still reached. console.log calls process.stdout.write under the
    // hood — so if our wrapper broke the call chain, this write wouldn't fire.
    const origWrite = process.stdout.write.bind(process.stdout);
    let stdoutBuf = "";
    process.stdout.write = (chunk) => {
      stdoutBuf += typeof chunk === "string" ? chunk : chunk.toString();
      return true;
    };
    try {
      const result = setupFileLog(dir);
      console.log("tee-me");
      await endStream(result.stream);
      assert.match(stdoutBuf, /tee-me/, "stdout path must still fire");
    } finally {
      process.stdout.write = origWrite;
    }
  });
});
