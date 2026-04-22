"use strict";

// Iter-22 P2-O1: verify readJsonBody destroys the socket on overflow so a
// slow-drip client can't hold the connection for the full request timeout.
//
// Run with: node --test tests/sidecar_readjsonbody_destroy.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const net = require("node:net");

const path = require("node:path");
const sidecar = require(path.resolve(
  __dirname,
  "..",
  "src",
  "clawcu",
  "a2a",
  "sidecar_plugin",
  "openclaw",
  "sidecar",
  "server.js",
));

test("readJsonBody destroys the request socket on body overflow", async () => {
  // Stand up a tiny server that delegates to readJsonBody with a 1 KiB limit.
  // If the body exceeds that, readJsonBody should call req.destroy(), which
  // closes the underlying socket. The client should see ECONNRESET quickly.
  const srv = http.createServer((req, res) => {
    sidecar.readJsonBody(req, 1024).then(
      () => res.end("ok"),
      () => {
        // Rejection expected — the socket should already be destroyed
        // by readJsonBody's req.destroy() call, so we can't write a
        // response. That's the point.
      },
    );
  });

  await new Promise((resolve) => srv.listen(0, "127.0.0.1", resolve));
  const port = srv.address().port;

  const start = Date.now();

  const closed = new Promise((resolve) => {
    const sock = net.createConnection(port, "127.0.0.1", () => {
      sock.write(
        "POST /test HTTP/1.1\r\n" +
          "Host: 127.0.0.1\r\n" +
          "Content-Type: application/json\r\n" +
          "Content-Length: 100000\r\n" +
          "\r\n",
      );
      // Send 2 KiB — exceeds the 1 KiB limit.
      sock.write("x".repeat(2048));
    });

    sock.on("close", () => {
      const elapsed = Date.now() - start;
      resolve(elapsed);
    });

    sock.on("error", () => {
      // ECONNRESET is expected.
      const elapsed = Date.now() - start;
      resolve(elapsed);
    });
  });

  const elapsed = await closed;
  // The socket should be destroyed within 1 s — well before any timeout
  // (default is 120 s). Without req.destroy(), the socket would linger.
  assert.ok(elapsed < 1000, `socket close took ${elapsed}ms, expected < 1000ms`);

  await new Promise((resolve) => srv.close(resolve));
});
