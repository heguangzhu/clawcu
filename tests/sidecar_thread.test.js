"use strict";

// Node-test unit tests for the openclaw sidecar thread-history store.
// Run with: node --test tests/sidecar_thread.test.js
//
// Pytest ignores this file (filename pattern doesn't match test_*.py). The
// module under test lives at
// src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/thread.js (review-13 P1-C).

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createThreadStore, safeId } = require(
  path.resolve(
    __dirname,
    "..",
    "src",
    "clawcu",
    "a2a",
    "sidecar_plugin",
    "openclaw",
    "sidecar",
    "thread.js",
  ),
);

function mkTempDir(label) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `clawcu-thread-${label}-`));
}

test("disabled store (no storageDir) is a no-op for load and append", () => {
  const store = createThreadStore({ storageDir: "" });
  assert.equal(store.enabled, false);
  assert.deepEqual(store.loadHistory("peer", "tid"), []);
  assert.equal(store.appendTurn("peer", "tid", "hi", "hello"), false);
});

test("loadHistory returns [] when the thread file does not exist", () => {
  const dir = mkTempDir("load-empty");
  const store = createThreadStore({ storageDir: dir });
  assert.deepEqual(store.loadHistory("peer-a", "thread-1"), []);
});

test("appendTurn then loadHistory roundtrips in order", () => {
  const dir = mkTempDir("roundtrip");
  const store = createThreadStore({ storageDir: dir });
  assert.equal(store.appendTurn("peer-a", "tid-1", "hi", "hello"), true);
  assert.equal(store.appendTurn("peer-a", "tid-1", "how are you", "fine"), true);
  const history = store.loadHistory("peer-a", "tid-1");
  assert.deepEqual(history, [
    { role: "user", content: "hi" },
    { role: "assistant", content: "hello" },
    { role: "user", content: "how are you" },
    { role: "assistant", content: "fine" },
  ]);
});

test("loadHistory caps at maxHistoryPairs * 2 from the tail", () => {
  const dir = mkTempDir("cap");
  const store = createThreadStore({ storageDir: dir, maxHistoryPairs: 2 });
  for (let i = 0; i < 5; i++) {
    store.appendTurn("peer-a", "tid-1", `u${i}`, `a${i}`);
  }
  const history = store.loadHistory("peer-a", "tid-1");
  // 2 pairs = last 4 messages.
  assert.equal(history.length, 4);
  assert.deepEqual(history, [
    { role: "user", content: "u3" },
    { role: "assistant", content: "a3" },
    { role: "user", content: "u4" },
    { role: "assistant", content: "a4" },
  ]);
});

test("path-traversal attempts are rejected (no file written, load returns [])", () => {
  const dir = mkTempDir("traversal");
  const store = createThreadStore({ storageDir: dir });
  // Slash, dotdot, and control chars all rejected by SAFE_ID.
  const attempts = [
    ["../escape", "tid"],
    ["peer", "../escape"],
    ["peer/sub", "tid"],
    ["peer", "tid/sub"],
    ["..", "tid"],
    ["peer", ".."],
    ["", "tid"],
    ["peer", ""],
  ];
  for (const [peer, tid] of attempts) {
    assert.equal(store.appendTurn(peer, tid, "x", "y"), false, `append ${peer}/${tid}`);
    assert.deepEqual(store.loadHistory(peer, tid), [], `load ${peer}/${tid}`);
  }
  // Storage dir has no sibling created.
  const siblings = fs.readdirSync(path.dirname(dir));
  assert.ok(
    !siblings.some((n) => n.startsWith("escape")),
    "no files escaped storageDir",
  );
});

test("per-peer isolation: two peers with the same thread_id get separate files", () => {
  const dir = mkTempDir("isolation");
  const store = createThreadStore({ storageDir: dir });
  store.appendTurn("peer-a", "tid-1", "A-msg", "A-reply");
  store.appendTurn("peer-b", "tid-1", "B-msg", "B-reply");
  assert.deepEqual(store.loadHistory("peer-a", "tid-1"), [
    { role: "user", content: "A-msg" },
    { role: "assistant", content: "A-reply" },
  ]);
  assert.deepEqual(store.loadHistory("peer-b", "tid-1"), [
    { role: "user", content: "B-msg" },
    { role: "assistant", content: "B-reply" },
  ]);
});

test("corrupt JSON line is skipped but valid lines around it load", () => {
  const dir = mkTempDir("corrupt");
  const store = createThreadStore({ storageDir: dir });
  store.appendTurn("peer-a", "tid-1", "hi", "hello");
  // Corrupt the file by hand: insert a non-json line between pairs.
  const file = path.join(dir, "peer-a", "tid-1.jsonl");
  fs.appendFileSync(file, "this is not json\n", "utf8");
  store.appendTurn("peer-a", "tid-1", "still there?", "yes");
  const history = store.loadHistory("peer-a", "tid-1");
  assert.deepEqual(history, [
    { role: "user", content: "hi" },
    { role: "assistant", content: "hello" },
    { role: "user", content: "still there?" },
    { role: "assistant", content: "yes" },
  ]);
});

test("non-string content is rejected (append returns false)", () => {
  const dir = mkTempDir("nonstring");
  const store = createThreadStore({ storageDir: dir });
  assert.equal(store.appendTurn("peer-a", "tid-1", 42, "ok"), false);
  assert.equal(store.appendTurn("peer-a", "tid-1", "ok", null), false);
  assert.deepEqual(store.loadHistory("peer-a", "tid-1"), []);
});

test("safeId accepts typical UUID v7 strings and rejects dangerous ones", () => {
  assert.equal(safeId("0194c3f0-7d1a-7a3e-8b8e-7e0e7a1f6d42"), "0194c3f0-7d1a-7a3e-8b8e-7e0e7a1f6d42");
  assert.equal(safeId("peer.name_01"), "peer.name_01");
  assert.equal(safeId(""), null);
  assert.equal(safeId("."), null);
  assert.equal(safeId(".."), null);
  assert.equal(safeId("peer/with/slash"), null);
  assert.equal(safeId("peer with space"), null);
  assert.equal(safeId(null), null);
  assert.equal(safeId(undefined), null);
  assert.equal(safeId("x".repeat(129)), null, "length cap at 128");
});
