"use strict";

// Node-test unit tests for the openclaw sidecar's outbound primitive
// (a2a-design-1.md). Run with:
//   node --test tests/sidecar_outbound.test.js
//
// Scope:
//   - lookupPeer: 404 / 503 / ok paths against a stub registry.
//   - forwardToPeer: 2xx / 508 / 502 shape + hop-header propagation.
//   - readHopHeader: parsing boundary cases.
//
// Keep the test registry/peer in this file (tiny stdlib http servers) so
// nothing needs the docker mesh. End-to-end with the sidecar main()
// binding a port is not exercised here — that lives in the pytest suite
// (test_a2a.py) which already knows how to stand the full sidecar up.

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
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({ server, url: `http://127.0.0.1:${port}` });
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}

// -- lookupPeer --------------------------------------------------------------

test("lookupPeer: returns card on 200", async () => {
  const card = {
    name: "analyst",
    role: "hermes",
    skills: ["chat"],
    endpoint: "http://127.0.0.1:9129/a2a/send",
  };
  const { server, url } = await startServer((req, res) => {
    assert.equal(req.url, "/agents/analyst");
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(card));
  });
  try {
    const got = await sidecar.lookupPeer({
      registryUrl: url,
      peerName: "analyst",
      timeoutMs: 2000,
    });
    assert.deepEqual(got, card);
  } finally {
    await closeServer(server);
  }
});

test("lookupPeer: 404 from registry → httpStatus=404", async () => {
  const { server, url } = await startServer((_req, res) => {
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "not_found" }));
  });
  try {
    await assert.rejects(
      () =>
        sidecar.lookupPeer({
          registryUrl: url,
          peerName: "missing",
          timeoutMs: 2000,
        }),
      (err) => err.httpStatus === 404 && /not found/.test(err.message),
    );
  } finally {
    await closeServer(server);
  }
});

test("lookupPeer: non-2xx non-404 → httpStatus=503", async () => {
  const { server, url } = await startServer((_req, res) => {
    res.writeHead(500);
    res.end("boom");
  });
  try {
    await assert.rejects(
      () =>
        sidecar.lookupPeer({
          registryUrl: url,
          peerName: "analyst",
          timeoutMs: 2000,
        }),
      (err) => err.httpStatus === 503,
    );
  } finally {
    await closeServer(server);
  }
});

test("lookupPeer: card missing endpoint → httpStatus=503", async () => {
  const { server, url } = await startServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ name: "analyst" }));
  });
  try {
    await assert.rejects(
      () =>
        sidecar.lookupPeer({
          registryUrl: url,
          peerName: "analyst",
          timeoutMs: 2000,
        }),
      (err) => err.httpStatus === 503 && /endpoint/.test(err.message),
    );
  } finally {
    await closeServer(server);
  }
});

// -- forwardToPeer -----------------------------------------------------------

test("forwardToPeer: 2xx returns parsed body, carries hop header", async () => {
  let observedHop = null;
  let observedBody = null;
  const { server, url } = await startServer((req, res) => {
    observedHop = req.headers["x-a2a-hop"];
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      observedBody = JSON.parse(raw);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ from: "analyst", reply: "42", thread_id: null }));
    });
  });
  try {
    const got = await sidecar.forwardToPeer({
      endpoint: `${url}/a2a/send`,
      selfName: "writer",
      peerName: "analyst",
      message: "hi",
      threadId: null,
      hop: 3,
      timeoutMs: 2000,
    });
    assert.equal(got.reply, "42");
    assert.equal(observedHop, "3");
    assert.equal(observedBody.from, "writer");
    assert.equal(observedBody.to, "analyst");
    assert.equal(observedBody.message, "hi");
    assert.equal("thread_id" in observedBody, false);
  } finally {
    await closeServer(server);
  }
});

test("forwardToPeer: thread_id propagates when present", async () => {
  let observedBody = null;
  const { server, url } = await startServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      observedBody = JSON.parse(raw);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ from: "analyst", reply: "k", thread_id: "t-1" }));
    });
  });
  try {
    await sidecar.forwardToPeer({
      endpoint: `${url}/a2a/send`,
      selfName: "writer",
      peerName: "analyst",
      message: "hi",
      threadId: "t-1",
      hop: 1,
      timeoutMs: 2000,
    });
    assert.equal(observedBody.thread_id, "t-1");
  } finally {
    await closeServer(server);
  }
});

test("forwardToPeer: peer 508 surfaces httpStatus=508", async () => {
  const { server, url } = await startServer((_req, res) => {
    res.writeHead(508, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "hop budget exceeded" }));
  });
  try {
    await assert.rejects(
      () =>
        sidecar.forwardToPeer({
          endpoint: `${url}/a2a/send`,
          selfName: "writer",
          peerName: "analyst",
          message: "hi",
          threadId: null,
          hop: 9,
          timeoutMs: 2000,
        }),
      (err) => err.httpStatus === 508,
    );
  } finally {
    await closeServer(server);
  }
});

test("forwardToPeer: peer 500 maps to httpStatus=502", async () => {
  const { server, url } = await startServer((_req, res) => {
    res.writeHead(500);
    res.end("boom");
  });
  try {
    await assert.rejects(
      () =>
        sidecar.forwardToPeer({
          endpoint: `${url}/a2a/send`,
          selfName: "writer",
          peerName: "analyst",
          message: "hi",
          threadId: null,
          hop: 1,
          timeoutMs: 2000,
        }),
      (err) => err.httpStatus === 502 && err.peerStatus === 500,
    );
  } finally {
    await closeServer(server);
  }
});

// -- P1-C socket-error status unification (iter 3) ---------------------------
//
// Network-layer failures (connect refused, DNS, timeout) should map to 504;
// peer HTTP errors keep mapping to 502. Before iter 3 these cases fell
// through to 502 indistinctly, confusing grep-based debugging.

test("forwardToPeer: connection refused → httpStatus=504 (network layer)", async () => {
  // Bind-then-close guarantees the port is unused for the duration of the test.
  const { server, url } = await startServer((_req, res) => {
    res.writeHead(200);
    res.end("unused");
  });
  await closeServer(server);
  const refusedEndpoint = `${url}/a2a/send`;
  await assert.rejects(
    () =>
      sidecar.forwardToPeer({
        endpoint: refusedEndpoint,
        selfName: "writer",
        peerName: "analyst",
        message: "hi",
        threadId: null,
        hop: 1,
        timeoutMs: 2000,
      }),
    (err) => err.httpStatus === 504,
  );
});

test("forwardToPeer: request timeout → httpStatus=504", async () => {
  // Server accepts the connection but never responds; the client timeout
  // triggers the network-layer failure path.
  const { server, url } = await startServer((_req, _res) => {
    // hang forever — response is never sent.
  });
  try {
    await assert.rejects(
      () =>
        sidecar.forwardToPeer({
          endpoint: `${url}/a2a/send`,
          selfName: "writer",
          peerName: "analyst",
          message: "hi",
          threadId: null,
          hop: 1,
          timeoutMs: 200,
        }),
      (err) => err.httpStatus === 504,
    );
  } finally {
    await closeServer(server);
  }
});

// -- readHopHeader -----------------------------------------------------------

test("readHopHeader: absent header → 0", () => {
  assert.equal(sidecar.readHopHeader({ headers: {} }), 0);
});

test("readHopHeader: valid integer parses", () => {
  assert.equal(sidecar.readHopHeader({ headers: { "x-a2a-hop": "3" } }), 3);
});

test("readHopHeader: negative rejected → 0", () => {
  assert.equal(sidecar.readHopHeader({ headers: { "x-a2a-hop": "-1" } }), 0);
});

test("readHopHeader: garbage → 0", () => {
  assert.equal(
    sidecar.readHopHeader({ headers: { "x-a2a-hop": "abc" } }),
    0,
  );
});

test("readHopHeader: float truncates to int", () => {
  assert.equal(sidecar.readHopHeader({ headers: { "x-a2a-hop": "2.9" } }), 2);
});

// -- parseHttpUrl ------------------------------------------------------------

test("parseHttpUrl: default port 80 for http", () => {
  const p = sidecar.parseHttpUrl("http://host.docker.internal/agents");
  assert.equal(p.host, "host.docker.internal");
  assert.equal(p.port, 80);
});

test("parseHttpUrl: explicit port wins", () => {
  const p = sidecar.parseHttpUrl("http://127.0.0.1:8765");
  assert.equal(p.port, 8765);
  assert.equal(p.pathname, "/");
});

test("parseHttpUrl: rejects file://", () => {
  assert.throws(
    () => sidecar.parseHttpUrl("file:///etc/hosts"),
    /unsupported protocol/,
  );
});

// -- request_id correlation (review-2 P1-D) ---------------------------------

test("readOrMintRequestId: uses caller-supplied header when valid", () => {
  const id = sidecar.readOrMintRequestId({
    headers: { "x-a2a-request-id": "abc-123" },
  });
  assert.equal(id, "abc-123");
});

test("readOrMintRequestId: trims whitespace", () => {
  const id = sidecar.readOrMintRequestId({
    headers: { "x-a2a-request-id": "  zzz  " },
  });
  assert.equal(id, "zzz");
});

test("readOrMintRequestId: mints fresh id when header missing", () => {
  const id = sidecar.readOrMintRequestId({ headers: {} });
  assert.equal(typeof id, "string");
  assert.ok(id.length >= 16, `expected minted id long enough, got '${id}'`);
});

test("readOrMintRequestId: rejects control chars and mints fresh", () => {
  const id = sidecar.readOrMintRequestId({
    headers: { "x-a2a-request-id": "bad\nvalue" },
  });
  assert.notEqual(id, "bad\nvalue");
  assert.equal(typeof id, "string");
});

test("readOrMintRequestId: rejects values >128 chars and mints fresh", () => {
  const tooBig = "x".repeat(200);
  const id = sidecar.readOrMintRequestId({
    headers: { "x-a2a-request-id": tooBig },
  });
  assert.notEqual(id, tooBig);
  assert.ok(id.length <= 64);
});

test("forwardToPeer: forwards X-A2A-Request-Id header when provided", async () => {
  let seenHeader = null;
  const { server, url } = await startServer((req, res) => {
    seenHeader = req.headers["x-a2a-request-id"] || null;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ from: "peer", reply: "ok", thread_id: null }));
  });
  try {
    await sidecar.forwardToPeer({
      endpoint: `${url}/a2a/send`,
      selfName: "caller",
      peerName: "peer",
      message: "hi",
      threadId: null,
      hop: 1,
      timeoutMs: 2000,
      requestId: "corr-42",
    });
    assert.equal(seenHeader, "corr-42");
  } finally {
    await closeServer(server);
  }
});

test("forwardToPeer: omits request_id header when none supplied", async () => {
  let seenHeader = "uninitialized";
  const { server, url } = await startServer((req, res) => {
    seenHeader = req.headers["x-a2a-request-id"];
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ from: "peer", reply: "ok", thread_id: null }));
  });
  try {
    await sidecar.forwardToPeer({
      endpoint: `${url}/a2a/send`,
      selfName: "caller",
      peerName: "peer",
      message: "hi",
      threadId: null,
      hop: 1,
      timeoutMs: 2000,
    });
    assert.equal(seenHeader, undefined);
  } finally {
    await closeServer(server);
  }
});
