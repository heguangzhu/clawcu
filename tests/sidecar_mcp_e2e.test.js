"use strict";

// End-to-end test for the sidecar's /mcp route (a2a-design-4.md §P1-G).
//
// Spins up:
//   - stub registry (stdlib http) that maps /agents/<name> → AgentCard
//   - stub peer (stdlib http) that replies to /a2a/send
//   - real sidecar main() as a child process, pointed at the stub registry
//
// Asserts:
//   - POST /mcp initialize → protocolVersion in result
//   - POST /mcp tools/list → a2a_call_peer in tools[]
//   - POST /mcp tools/call happy → content[0].text matches stub reply
//   - POST /mcp tools/call unknown peer → JSON-RPC error, httpStatus=404
//
// We exercise the binary because the /mcp handler is defined inside main()
// as a closure over `selfName`, `rateLimiter`, and the shared outbound
// limiter — unit testing the module export wouldn't cover the wiring that
// matters here (review-3 P1-G).

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");

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

function startServer(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({ server, port, url: `http://127.0.0.1:${port}` });
    });
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let buf = "";
    req.on("data", (c) => (buf += c));
    req.on("end", () => resolve(buf));
    req.on("error", reject);
  });
}

function postJson(url, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = http.request(
      {
        method: "POST",
        hostname: u.hostname,
        port: u.port,
        path: u.pathname,
        headers: { "content-type": "application/json", ...headers },
      },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          let parsed = null;
          try {
            parsed = JSON.parse(buf);
          } catch {
            /* leave null */
          }
          resolve({ status: res.statusCode, headers: res.headers, body: parsed, raw: buf });
        });
      },
    );
    req.on("error", reject);
    req.end(JSON.stringify(body));
  });
}

async function waitForPort(port, deadlineMs = 5000) {
  // Poll /.well-known/agent-card.json — the sidecar's first public route.
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    try {
      await new Promise((resolve, reject) => {
        const req = http.request(
          {
            method: "GET",
            hostname: "127.0.0.1",
            port,
            path: "/.well-known/agent-card.json",
            timeout: 500,
          },
          (res) => {
            res.resume();
            res.on("end", () => resolve());
          },
        );
        req.on("error", reject);
        req.on("timeout", () => req.destroy(new Error("timeout")));
        req.end();
      });
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  throw new Error(`sidecar didn't bind :${port} within ${deadlineMs}ms`);
}

function pickPort() {
  return new Promise((resolve, reject) => {
    const srv = http.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

test("mcp-e2e: initialize, tools/list, tools/call happy + unknown-peer paths", async (t) => {
  // --- stub peer ---
  const { server: peerServer, url: peerUrl } = await startServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/a2a/send") {
      res.writeHead(404).end();
      return;
    }
    const body = JSON.parse(await readBody(req));
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ from: body.to, reply: `pong:${body.message}` }));
  });
  t.after(() => closeServer(peerServer));

  // --- stub registry ---
  const { server: regServer, url: regUrl } = await startServer(async (req, res) => {
    const m = /^\/agents\/([^/?]+)/.exec(req.url || "");
    if (!m) {
      res.writeHead(404).end();
      return;
    }
    const name = decodeURIComponent(m[1]);
    if (name === "ghost") {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "not_found" }));
      return;
    }
    if (name !== "analyst") {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(
      JSON.stringify({
        name: "analyst",
        role: "analyst",
        skills: ["data"],
        endpoint: `${peerUrl}/a2a/send`,
      }),
    );
  });
  t.after(() => closeServer(regServer));

  // --- sidecar process ---
  const port = await pickPort();
  const child = spawn(
    process.execPath,
    [SIDECAR_PATH, "--local", "--port", String(port), "--name", "writer"],
    {
      env: {
        ...process.env,
        A2A_REGISTRY_URL: regUrl,
        CLAWCU_PLUGIN_VERSION: "e2e-test",
        A2A_GATEWAY_READY_DEADLINE_MS: "0",
        // Never wire an MCP config file during tests.
        A2A_SERVICE_MCP_CONFIG_PATH: "",
        A2A_ENABLED: "false",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const stdoutChunks = [];
  const stderrChunks = [];
  child.stdout.on("data", (c) => stdoutChunks.push(c));
  child.stderr.on("data", (c) => stderrChunks.push(c));
  t.after(async () => {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      await new Promise((r) => child.once("exit", r));
    }
  });

  try {
    await waitForPort(port);
  } catch (e) {
    throw new Error(
      `${e.message}\nstdout: ${Buffer.concat(stdoutChunks).toString()}\nstderr: ${Buffer.concat(stderrChunks).toString()}`,
    );
  }

  const mcpUrl = `http://127.0.0.1:${port}/mcp`;

  // --- initialize ---
  const init = await postJson(mcpUrl, {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
  });
  assert.equal(init.status, 200);
  assert.equal(init.body.result.protocolVersion, "2024-11-05");
  assert.equal(init.body.result.serverInfo.name, "clawcu-a2a");
  assert.equal(init.body.result.serverInfo.version, "e2e-test");

  // --- tools/list ---
  const list = await postJson(mcpUrl, {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/list",
  });
  assert.equal(list.status, 200);
  const tools = list.body.result.tools;
  assert.equal(tools.length, 1);
  assert.equal(tools[0].name, "a2a_call_peer");

  // --- tools/call happy ---
  const call = await postJson(mcpUrl, {
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: {
      name: "a2a_call_peer",
      arguments: { to: "analyst", message: "hello" },
    },
  });
  assert.equal(call.status, 200);
  assert.equal(call.body.result.isError, false);
  assert.equal(call.body.result.content[0].text, "pong:hello");
  // request-id correlation header round-trip
  assert.ok(call.headers["x-a2a-request-id"], "request-id header echoed");

  // --- tools/call unknown peer ---
  const ghost = await postJson(mcpUrl, {
    jsonrpc: "2.0",
    id: 4,
    method: "tools/call",
    params: {
      name: "a2a_call_peer",
      arguments: { to: "ghost", message: "hi" },
    },
  });
  assert.equal(ghost.status, 200, "HTTP 200 — JSON-RPC error lives in body");
  assert.ok(ghost.body.error, "error object present");
  assert.equal(ghost.body.error.code, -32001);
  assert.equal(ghost.body.error.data.httpStatus, 404);
});
