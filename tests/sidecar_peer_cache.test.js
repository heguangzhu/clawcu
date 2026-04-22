"use strict";

// Unit tests for fetchPeerList + createPeerCache (a2a-design-5.md §P1-H).
// Scope: TTL freshness, deduping concurrent fetches, stale-OK fallback on
// registry failure, null fallback after stale expiry.

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");

const sidecar = require(
  path.resolve(
    __dirname,
    "..",
    "src",
    "clawcu",
    "a2a",
    "sidecar_plugin",
    "openclaw",
    "sidecar",
    "server.js",
  ),
);

function startServer(handler) {
  return new Promise((resolve) => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      resolve({ srv, url: `http://127.0.0.1:${port}` });
    });
  });
}

test("fetchPeerList: returns array on 200", async () => {
  const { srv, url } = await startServer((req, res) => {
    assert.equal(req.url, "/agents");
    res.writeHead(200, { "content-type": "application/json" });
    res.end(
      JSON.stringify([
        { name: "a", role: "r", skills: ["s"] },
        { name: "b", role: "r2", skills: [] },
      ]),
    );
  });
  try {
    const peers = await sidecar.fetchPeerList({ registryUrl: url, timeoutMs: 2000 });
    assert.equal(peers.length, 2);
    assert.equal(peers[0].name, "a");
  } finally {
    srv.close();
  }
});

test("fetchPeerList: returns null on 404", async () => {
  const { srv, url } = await startServer((_req, res) => {
    res.writeHead(404).end();
  });
  try {
    const peers = await sidecar.fetchPeerList({ registryUrl: url, timeoutMs: 2000 });
    assert.equal(peers, null);
  } finally {
    srv.close();
  }
});

test("fetchPeerList: returns null on non-JSON body", async () => {
  const { srv, url } = await startServer((_req, res) => {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("not json");
  });
  try {
    const peers = await sidecar.fetchPeerList({ registryUrl: url, timeoutMs: 2000 });
    assert.equal(peers, null);
  } finally {
    srv.close();
  }
});

test("fetchPeerList: returns null on non-array response", async () => {
  const { srv, url } = await startServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"peers":[]}');
  });
  try {
    const peers = await sidecar.fetchPeerList({ registryUrl: url, timeoutMs: 2000 });
    assert.equal(peers, null);
  } finally {
    srv.close();
  }
});

test("fetchPeerList: filters entries without a name", async () => {
  const { srv, url } = await startServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify([{ name: "a" }, { role: "r" }, null, { name: "c" }]));
  });
  try {
    const peers = await sidecar.fetchPeerList({ registryUrl: url, timeoutMs: 2000 });
    assert.deepEqual(
      peers.map((p) => p.name),
      ["a", "c"],
    );
  } finally {
    srv.close();
  }
});

test("createPeerCache: serves cached result within TTL window", async () => {
  let calls = 0;
  const cache = sidecar.createPeerCache({
    registryUrl: "http://stub",
    timeoutMs: 2000,
    freshMs: 30_000,
    nowFn: () => 1000,
    fetchFn: async () => {
      calls += 1;
      return [{ name: "a" }];
    },
  });
  const r1 = await cache.get();
  const r2 = await cache.get();
  assert.equal(calls, 1, "second call within TTL must not refetch");
  assert.deepEqual(r1, r2);
});

test("createPeerCache: refetches after TTL expires", async () => {
  let calls = 0;
  let now = 1000;
  const cache = sidecar.createPeerCache({
    registryUrl: "http://stub",
    timeoutMs: 2000,
    freshMs: 30_000,
    nowFn: () => now,
    fetchFn: async () => {
      calls += 1;
      return [{ name: "a" }];
    },
  });
  await cache.get();
  now += 31_000;
  await cache.get();
  assert.equal(calls, 2, "past TTL a refetch is expected");
});

test("createPeerCache: serves stale on fetch failure inside stale-OK window", async () => {
  let calls = 0;
  let now = 1000;
  const cache = sidecar.createPeerCache({
    registryUrl: "http://stub",
    timeoutMs: 2000,
    freshMs: 30_000,
    staleMs: 300_000,
    nowFn: () => now,
    fetchFn: async () => {
      calls += 1;
      return calls === 1 ? [{ name: "a" }] : null;
    },
  });
  const r1 = await cache.get();
  assert.deepEqual(r1, [{ name: "a" }]);
  now += 60_000; // past fresh but inside stale window
  const r2 = await cache.get();
  assert.deepEqual(r2, [{ name: "a" }], "stale cached copy still served");
});

test("createPeerCache: after stale window, returns null on continued failure", async () => {
  let now = 1000;
  const cache = sidecar.createPeerCache({
    registryUrl: "http://stub",
    timeoutMs: 2000,
    freshMs: 30_000,
    staleMs: 300_000,
    nowFn: () => now,
    fetchFn: async () => (now === 1000 ? [{ name: "a" }] : null),
  });
  await cache.get();
  now += 400_000; // past stale
  const r = await cache.get();
  assert.equal(r, null);
});

test("createPeerCache: dedupes concurrent in-flight fetches", async () => {
  let calls = 0;
  const cache = sidecar.createPeerCache({
    registryUrl: "http://stub",
    timeoutMs: 2000,
    freshMs: 30_000,
    nowFn: () => 1000,
    fetchFn: async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 20));
      return [{ name: "a" }];
    },
  });
  const [r1, r2, r3] = await Promise.all([cache.get(), cache.get(), cache.get()]);
  assert.equal(calls, 1, "three concurrent get()s should trigger one fetch");
  assert.deepEqual(r1, r2);
  assert.deepEqual(r2, r3);
});
