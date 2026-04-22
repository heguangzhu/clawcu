"use strict";

// Iter-21 P2-M1: outbound response body cap. postJson / httpRequestRaw
// (and therefore lookupPeer / forwardToPeer / fetchPeerList) must refuse
// to buffer responses larger than A2A_MAX_RESPONSE_BYTES so a compromised
// peer or registry can't OOM the sidecar. Direct in-process test of the
// exported helpers against a stub server that streams oversized bodies.
//
// Run with: node --test tests/sidecar_outbound_size_cap.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");

const SIDECAR_PATH = path.resolve(
  __dirname,
  "..",
  "src",
  "clawcu",
  "a2a",
  "sidecar_plugin",
  "openclaw",
  "sidecar",
  "server.js",
);

const sidecar = require(SIDECAR_PATH);

function startOversizedServer() {
  // Stream 5 MiB (> the 4 MiB cap). Absorbs any POST body first so postJson
  // can complete its write side before reading.
  const OVERSIZE = 5 * 1024 * 1024;
  const server = http.createServer((req, res) => {
    const drain = () =>
      new Promise((resolve) => {
        req.on("data", () => {});
        req.on("end", resolve);
        req.on("error", resolve);
      });
    drain().then(() => {
      res.writeHead(200, {
        "content-type": "application/json",
        "content-length": String(OVERSIZE),
      });
      const chunk = Buffer.alloc(65536, "a");
      let sent = 0;
      const pump = () => {
        while (sent < OVERSIZE) {
          const n = Math.min(chunk.length, OVERSIZE - sent);
          const ok = res.write(chunk.slice(0, n));
          sent += n;
          if (!ok) {
            res.once("drain", pump);
            return;
          }
        }
        res.end();
      };
      pump();
      // Ignore EPIPE when client disconnects on overflow.
      res.on("error", () => {});
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, port: server.address().port });
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

test("outbound-size: lookupPeer rejects oversized registry response", async () => {
  const { server, port } = await startOversizedServer();
  try {
    await assert.rejects(
      sidecar.lookupPeer({
        registryUrl: `http://127.0.0.1:${port}`,
        peerName: "analyst",
        timeoutMs: 5000,
      }),
      /exceeds/,
    );
  } finally {
    await closeServer(server);
  }
});

test("outbound-size: forwardToPeer rejects oversized peer response", async () => {
  const { server, port } = await startOversizedServer();
  try {
    // postJson rejects with the "exceeds" message; forwardToPeer catches it
    // into OutboundError "peer unreachable or timed out: …" (httpStatus=504).
    // Either surfacing is acceptable; the key guarantee is we didn't buffer
    // the oversized body into memory.
    await assert.rejects(
      sidecar.forwardToPeer({
        endpoint: `http://127.0.0.1:${port}/a2a/send`,
        selfName: "writer",
        peerName: "analyst",
        message: "hi",
        threadId: null,
        hop: 1,
        timeoutMs: 5000,
      }),
      /exceeds|unreachable/,
    );
  } finally {
    await closeServer(server);
  }
});

test("outbound-size: A2A_MAX_RESPONSE_BYTES is 4 MiB", () => {
  assert.equal(sidecar.A2A_MAX_RESPONSE_BYTES, 4 * 1024 * 1024);
});
