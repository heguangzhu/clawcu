"use strict";

// Iter-18 P1-J1: SSRF parity with the Python sidecar. /a2a/outbound's
// client-supplied `registry_url` body field must be rejected by default
// (operator opts in via A2A_ALLOW_CLIENT_REGISTRY_URL=1). Mirrors the
// shape of test_hermes_sidecar_client_registry_url_rejected_by_default
// + test_hermes_sidecar_outbound_rejects_non_http_registry_scheme in
// tests/test_a2a.py.
//
// Run with: node --test tests/sidecar_outbound_ssrf.test.js

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

async function startSidecar(t, extraEnv = {}) {
  const port = await pickPort();
  const child = spawn(
    process.execPath,
    [SIDECAR_PATH, "--local", "--port", String(port), "--name", "writer"],
    {
      env: {
        ...process.env,
        A2A_REGISTRY_URL: "http://127.0.0.1:0", // placeholder; overridden by body in happy-path test
        CLAWCU_PLUGIN_VERSION: "e2e-test",
        A2A_GATEWAY_READY_DEADLINE_MS: "0",
        A2A_SERVICE_MCP_CONFIG_PATH: "",
        A2A_ENABLED: "false",
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const stderrChunks = [];
  child.stderr.on("data", (c) => stderrChunks.push(c));
  t.after(async () => {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      await new Promise((r) => child.once("exit", r));
    }
  });
  await waitForPort(port);
  return { child, port, url: `http://127.0.0.1:${port}`, stderrChunks };
}

test("outbound-ssrf: body.registry_url rejected when flag is unset", async (t) => {
  const { url } = await startSidecar(t);
  const resp = await postJson(`${url}/a2a/outbound`, {
    to: "attacker",
    message: "probe",
    registry_url: "http://attacker.example/registry",
  });
  assert.equal(resp.status, 400);
  assert.ok(resp.body, "expected JSON body");
  assert.match(resp.body.error, /disabled by server policy/);
  assert.ok(typeof resp.body.request_id === "string" && resp.body.request_id);
});

test("outbound-ssrf: body.registry_url allowed when A2A_ALLOW_CLIENT_REGISTRY_URL=1", async (t) => {
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
  let lookupCount = 0;
  const { server: regServer, url: regUrl } = await startServer(async (req, res) => {
    const m = /^\/agents\/([^/?]+)/.exec(req.url || "");
    if (!m) {
      res.writeHead(404).end();
      return;
    }
    lookupCount += 1;
    const name = decodeURIComponent(m[1]);
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

  const { url } = await startSidecar(t, {
    A2A_ALLOW_CLIENT_REGISTRY_URL: "1",
  });
  const resp = await postJson(`${url}/a2a/outbound`, {
    to: "analyst",
    message: "ping",
    registry_url: regUrl,
  });
  assert.equal(resp.status, 200, `body: ${resp.raw}`);
  assert.equal(resp.body.reply, "pong:ping");
  assert.equal(lookupCount, 1);
});

test("outbound-ssrf: body.registry_url with non-string type still 400 even with flag on", async (t) => {
  const { url } = await startSidecar(t, {
    A2A_ALLOW_CLIENT_REGISTRY_URL: "1",
  });
  const resp = await postJson(`${url}/a2a/outbound`, {
    to: "analyst",
    message: "ping",
    registry_url: null,
  });
  assert.equal(resp.status, 400);
  assert.match(resp.body.error, /registry_url.*non-empty string/);
});

test("readAllowClientRegistryUrl parses the env var", () => {
  const sidecar = require("../src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/server.js");
  assert.equal(sidecar.readAllowClientRegistryUrl({}), false);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: "" }), false);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: "0" }), false);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: "false" }), false);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: "1" }), true);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: "true" }), true);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: "YES" }), true);
  assert.equal(sidecar.readAllowClientRegistryUrl({ A2A_ALLOW_CLIENT_REGISTRY_URL: " on " }), true);
});
