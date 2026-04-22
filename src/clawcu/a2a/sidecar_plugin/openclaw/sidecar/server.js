#!/usr/bin/env node
// a2a-bridge sidecar for OpenClaw instances.
//
// Architecture (iter3 — native-agent routing):
//   The sidecar forwards /a2a/send to the gateway's own OpenAI-compatible
//   endpoint at /v1/chat/completions. That endpoint is handled by
//   gateway/server-methods/chat.ts via chat.send, which runs the full
//   OpenClaw agent turn (persona, skills, tools, provider) — so an A2A
//   peer gets the agent's "native" reply, not a bare LLM completion.
//
//   The endpoint is gated by `gateway.http.endpoints.chatCompletions.enabled`
//   which the clawcu openclaw adapter flips on whenever a2a_enabled=true.
//
// Authentication:
//   Gateway runs with auth.mode=token; the token is stored in
//   openclaw.json → gateway.auth.token. The sidecar reads that file at
//   request time and sends Authorization: Bearer <token>.
//
// Usage:
//   node server.js --local --port 18790 [--name <instance>]
//   node server.js --instance <name> --port 18820 [--container <name>]
//
// Config (env vars; all optional, baked-in defaults in parens):
//   A2A_GATEWAY_READY_DEADLINE_MS (default 30000) — upper bound in ms the
//     sidecar will wait for the gateway readiness path to become reachable
//     before answering /a2a/send with 503 "gateway not ready". Set to 0 for
//     immediate fail-fast. Must be << the upstream call timeout or
//     readiness eats the upstream budget.
//   A2A_GATEWAY_READY_PATH (default /healthz) — path probed on the gateway.
//     Injected by the adapter so the sidecar is gateway-agnostic
//     (review-7 P2-E: hermes uses /health, openclaw uses /healthz).
//   A2A_RATE_LIMIT_PER_MINUTE (default 30) — per-peer sliding-window rate
//     limit on /a2a/send. 0 disables. Review-9 P2-A.
//   A2A_SIDECAR_LOG_DIR (no default) — when set, console.log/error are
//     also teed to <dir>/a2a-sidecar.log so logs persist across
//     `clawcu recreate`. The adapter points it at the container-side
//     datadir mount so the file lands on the host datadir. Review-10 P2-C.
//   A2A_MODEL (default "openclaw") — value forwarded as the `model` field
//     on /v1/chat/completions. The openclaw gateway accepts "openclaw"
//     (default agent) and "openclaw/<agentId>" (route to a specific
//     configured agent). Review-12 P2-B: the adapter intentionally does
//     NOT set A2A_MODEL in docker --env, so a user-supplied value in the
//     instance env file wins. Set A2A_MODEL=openclaw/my-agent in the env
//     file to direct A2A traffic at a non-default agent.
//   A2A_THREAD_DIR (no default) — when set, /a2a/send reads an optional
//     `thread_id` field from the peer's payload and loads prior turns
//     from <dir>/<peer>/<thread_id>.jsonl so the native agent sees
//     continuous conversation context. Appended after the reply. Review-13
//     P1-C: a local extension on top of A2A v0.1; peers without thread_id
//     behave exactly as before.
//   A2A_THREAD_MAX_HISTORY_PAIRS (default 10) — cap on replayed
//     user+assistant pairs (= 20 messages) prepended to /v1/chat/completions.
//     Bounds token spend on long-running threads. File retains all turns.
//   Once healthy, the "ready" fact is cached for 5 minutes. The cache is
//   dropped on the next upstream failure that looks like gateway-down
//   (ECONNREFUSED / ECONNRESET / ETIMEDOUT / "socket hang up" / HTTP 5xx
//   / non-json body) so a gateway death mid-session is detected on the
//   following request instead of after the TTL (review-4 P1-A).

"use strict";

const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { URL } = require("node:url");
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");

// Gateway readiness primitives (probe / cache / invalidate) live in
// readiness.js so they can be node-unit-tested without bringing up the
// full HTTP server. Review-8 P1-G.
const {
  waitForGatewayReady,
  invalidateGatewayReady,
  looksLikeGatewayDown,
} = require(path.join(__dirname, "readiness.js"));

// Per-peer rate limiter sits in front of /a2a/send so a misbehaving peer
// can't burn provider quota by flooding us. Review-9 P2-A.
const { createRateLimiter } = require(path.join(__dirname, "ratelimit.js"));

// Tees console.log/error to <A2A_SIDECAR_LOG_DIR>/a2a-sidecar.log so the
// sidecar's own audit trail survives `clawcu recreate`. Review-10 P2-C.
const { setupFileLog } = require(path.join(__dirname, "logsink.js"));
setupFileLog(process.env.A2A_SIDECAR_LOG_DIR || "");

// MCP server (a2a-design-3.md §P0-A). Exposes /mcp on the same port so
// the LLM can call `a2a_call_peer` as a native tool. The handler reuses
// lookupPeer/forwardToPeer below, so every MCP tool call is an in-process
// function call, not a second HTTP hop.
const { handleMcpRequest } = require(path.join(__dirname, "mcp.js"));

// Auto-wire the `a2a` MCP entry into the OpenClaw config file on start
// (a2a-design-4.md §P0-A). Runs once just before server.listen; safe by
// construction (any failure logs a warning and continues).
const { runBootstrap: runMcpBootstrap } = require(path.join(__dirname, "bootstrap.js"));

// Self-origin outbound rate limit (a2a-design-4.md §P1-B). Hop budget caps
// depth; this caps breadth. Shared by /a2a/outbound and /mcp tool-call so
// one LLM turn can't nuke the provider quota by firing 200 parallel calls.
const {
  createOutboundLimiter,
  readRpm: readOutboundRpm,
  readSweepIntervalMs: readOutboundSweepIntervalMs,
  keyFor: outboundLimitKey,
  createSweepTimer: createOutboundSweepTimer,
} = require(path.join(__dirname, "outbound_limit.js"));

// Per-peer / per-thread conversation history so /a2a/send can carry
// context across turns when the peer supplies an optional thread_id.
// Review-13 P1-C.
const { createThreadStore } = require(path.join(__dirname, "thread.js"));

const OPENCLAW_CONFIG_PATH = "/home/node/.openclaw/openclaw.json";
// Fallback if openclaw ever migrates the token out of openclaw.json into a
// dedicated auth file. Review-4 P1-D: the sidecar was fragile against that
// migration — it only knew one path and would hard-fail if the layout moved.
const OPENCLAW_AUTH_PATH = "/home/node/.openclaw/auth.json";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) {
      out[key] = true;
    } else {
      out[key] = next;
      i++;
    }
  }
  return out;
}

// Source adapters let the same logic work whether we're running as a
// host-side sidecar (reading the container via `docker exec`) or baked
// into the image itself (reading the local filesystem directly).
function makeHostAdapter(container) {
  const exec = (args) => {
    try {
      return execFileSync("docker", ["exec", container, ...args], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch {
      return null;
    }
  };
  return {
    mode: "host",
    readFile(path) {
      return exec(["cat", path]);
    },
    getEnv(name) {
      const v = exec(["printenv", name]);
      return v ? v.trim() || null : null;
    },
  };
}

function makeLocalAdapter() {
  return {
    mode: "local",
    readFile(path) {
      try {
        return fs.readFileSync(path, "utf8");
      } catch {
        return null;
      }
    },
    getEnv(name) {
      const v = process.env[name];
      return v || null;
    },
  };
}

function readGatewayAuth(adapter) {
  const raw = adapter.readFile(OPENCLAW_CONFIG_PATH);
  if (!raw) throw new Error("could not read openclaw.json");
  const cfg = JSON.parse(raw);
  const authMode = cfg.gateway?.auth?.mode ?? "token";
  let token = cfg.gateway?.auth?.token ?? null;
  // Fallback: some openclaw versions (and future migrations) keep the token
  // in a dedicated auth.json under the same dir. Only consult it if the
  // primary file had no token — don't let the fallback override a real one.
  if (authMode === "token" && !token) {
    const authRaw = adapter.readFile(OPENCLAW_AUTH_PATH);
    if (authRaw) {
      try {
        const authCfg = JSON.parse(authRaw);
        token = authCfg?.gateway?.auth?.token ?? authCfg?.token ?? null;
      } catch {
        // Malformed auth.json — stay silent, fall through to the error below.
      }
    }
  }
  if (authMode === "token" && !token) {
    throw new Error(
      "gateway.auth.token missing in openclaw.json (and no fallback auth.json)",
    );
  }
  return { authMode, token };
}

// Review-21 P2-M1: cap outbound response body at 4 MiB so a
// compromised peer / registry can't stream GBs into the sidecar
// process and OOM it before the socket timeout fires. Applies to
// every outbound call (postJson and httpRequestRaw).
const A2A_MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

function readCappedBody(res, limit = A2A_MAX_RESPONSE_BYTES) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    let overflowed = false;
    res.on("data", (chunk) => {
      if (overflowed) return;
      total += chunk.length;
      if (total > limit) {
        overflowed = true;
        res.destroy();
        reject(new Error(`response exceeds ${limit} bytes`));
        return;
      }
      chunks.push(chunk);
    });
    res.on("end", () => {
      if (!overflowed) resolve(Buffer.concat(chunks).toString("utf8"));
    });
    res.on("error", (e) => {
      if (!overflowed) reject(e);
    });
  });
}

function postJson({ host, port, path, headers, bodyObj, timeoutMs = 120000 }) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(bodyObj);
    const req = http.request(
      {
        method: "POST",
        host,
        port,
        path,
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body),
          "user-agent": "a2a-bridge-sidecar/0.3",
          ...headers,
        },
      },
      (res) => {
        readCappedBody(res).then(
          (raw) => resolve({ status: res.statusCode ?? 0, body: raw }),
          (err) => reject(err),
        );
      }
    );
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`request timed out after ${timeoutMs}ms`));
    });
    req.write(body);
    req.end();
  });
}

// Parses an arbitrary absolute http URL to the pieces `postJson` / `http.request`
// want. Kept tolerant — the registry_url comes from env/config and typos
// shouldn't turn into unhandled throws inside the request handler.
function parseHttpUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (e) {
    throw new Error(`invalid url '${url}': ${e.message}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`unsupported protocol in '${url}'`);
  }
  const port = parsed.port
    ? Number(parsed.port)
    : parsed.protocol === "https:"
      ? 443
      : 80;
  return {
    host: parsed.hostname,
    port,
    pathname: parsed.pathname || "/",
    search: parsed.search || "",
  };
}

function httpRequestRaw({ method, host, port, path, headers, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { method, host, port, path, headers },
      (res) => {
        readCappedBody(res).then(
          (raw) => resolve({ status: res.statusCode ?? 0, body: raw }),
          (err) => reject(err),
        );
      },
    );
    req.on("error", reject);
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`request timed out after ${timeoutMs}ms`));
    });
    req.end();
  });
}

// Fetches the registry's full peer list. Registry contract: GET /agents
// returns a JSON array of {name, role, skills, endpoint} entries. On any
// failure (404, 5xx, network, non-json, non-array) returns null — callers
// are expected to fall back to a static description. See a2a-design-5.md
// §P1-H: tools/list must never fail because of a registry hiccup.
async function fetchPeerList({ registryUrl, timeoutMs }) {
  const { host, port, pathname } = parseHttpUrl(registryUrl);
  const base = pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
  const path = `${base}/agents`;
  let resp;
  try {
    resp = await httpRequestRaw({
      method: "GET",
      host,
      port,
      path,
      headers: { accept: "application/json", "user-agent": "a2a-bridge-sidecar/0.3" },
      timeoutMs,
    });
  } catch {
    return null;
  }
  if (resp.status < 200 || resp.status >= 300) return null;
  let parsed;
  try {
    parsed = JSON.parse(resp.body);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  return parsed.filter((p) => p && typeof p.name === "string");
}

// TTL cache on top of fetchPeerList. 30s fresh window, then a 5-minute
// "stale OK on registry failure" window before falling back to null.
// Shared by tools/list — an LLM refreshing tools every turn does not
// stampede the registry.
function createPeerCache({ registryUrl, timeoutMs, freshMs = 30_000, staleMs = 300_000, nowFn = Date.now, fetchFn = fetchPeerList }) {
  let cached = null;
  let fetchedAt = 0;
  let inflight = null;
  async function get() {
    const now = nowFn();
    if (cached && now - fetchedAt < freshMs) return cached;
    if (inflight) return inflight;
    inflight = (async () => {
      const got = await fetchFn({ registryUrl, timeoutMs });
      if (got !== null) {
        cached = got;
        fetchedAt = nowFn();
      } else if (cached && nowFn() - fetchedAt < staleMs) {
        // Keep serving the stale copy within the stale-OK window.
      } else {
        cached = null;
      }
      return cached;
    })();
    try {
      return await inflight;
    } finally {
      inflight = null;
    }
  }
  return { get };
}

// Outbound helpers — used by /a2a/outbound. Kept top-level so tests can
// import them without booting the full HTTP server.
async function lookupPeer({ registryUrl, peerName, timeoutMs }) {
  const { host, port, pathname } = parseHttpUrl(registryUrl);
  const base = pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
  const path = `${base}/agents/${encodeURIComponent(peerName)}`;
  const { status, body } = await httpRequestRaw({
    method: "GET",
    host,
    port,
    path,
    headers: { accept: "application/json", "user-agent": "a2a-bridge-sidecar/0.3" },
    timeoutMs,
  });
  if (status === 404) {
    const err = new Error(`peer '${peerName}' not found in registry`);
    err.httpStatus = 404;
    throw err;
  }
  if (status < 200 || status >= 300) {
    const err = new Error(`registry lookup ${status}: ${body.slice(0, 200)}`);
    err.httpStatus = 503;
    throw err;
  }
  let card;
  try {
    card = JSON.parse(body);
  } catch (e) {
    const err = new Error(`registry returned non-json: ${e.message}`);
    err.httpStatus = 503;
    throw err;
  }
  if (!card || typeof card.endpoint !== "string" || !card.endpoint) {
    const err = new Error(`registry card for '${peerName}' missing endpoint`);
    err.httpStatus = 503;
    throw err;
  }
  return card;
}

async function forwardToPeer({
  endpoint,
  selfName,
  peerName,
  message,
  threadId,
  hop,
  timeoutMs,
  requestId,
}) {
  const { host, port, pathname, search } = parseHttpUrl(endpoint);
  const bodyObj = { from: selfName, to: peerName, message };
  if (threadId) bodyObj.thread_id = threadId;
  const headers = { "x-a2a-hop": String(hop) };
  if (requestId) headers[REQUEST_ID_HEADER] = requestId;
  // Review-2 P1-C (iter 3): socket-layer failures (ECONNREFUSED, timeouts,
  // DNS) map to 504. Peer-reported HTTP errors below keep mapping to 502.
  // Unifies the status surface with /a2a/send so a single grep across
  // sidecar logs can separate "network is broken" from "peer is broken."
  let status, body;
  try {
    ({ status, body } = await postJson({
      host,
      port,
      path: pathname + search,
      headers,
      bodyObj,
      timeoutMs,
    }));
  } catch (e) {
    const err = new Error(`peer unreachable or timed out: ${e.message}`);
    err.httpStatus = 504;
    throw err;
  }
  if (status >= 200 && status < 300) {
    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch (e) {
      const err = new Error(`peer returned non-json: ${e.message}`);
      err.httpStatus = 502;
      throw err;
    }
    return parsed;
  }
  if (status === 508) {
    const err = new Error(`peer rejected hop limit: ${body.slice(0, 200)}`);
    err.httpStatus = 508;
    throw err;
  }
  if (status === 429) {
    const err = new Error(`peer rate-limited: ${body.slice(0, 200)}`);
    err.httpStatus = 429;
    throw err;
  }
  const err = new Error(`peer HTTP ${status}: ${body.slice(0, 200)}`);
  err.httpStatus = 502;
  err.peerStatus = status;
  err.peerBody = body;
  throw err;
}

// Loop protection — see a2a-design-1.md §Loop protection. X-A2A-Hop is an
// integer header that increments on every hop across the mesh. An inbound
// /a2a/send reads the incoming value (0 if absent), and if it's already
// equal to or past the budget we refuse with 508. /a2a/outbound forwards
// the value + 1 to the peer so downstream sidecars keep counting.
const A2A_HOP_BUDGET = Number(process.env.A2A_HOP_BUDGET || 8);
const OUTBOUND_LIMITER = createOutboundLimiter({ rpm: readOutboundRpm(process.env) });
// a2a-design-7.md §P1-N: the sweep timer is wired inside main() (see
// below), not here, so `require("server.js")` from a test file never
// starts a real setInterval. The limiter itself stays module-scope
// because handlers close over it.

function readHopHeader(req) {
  const raw = req.headers["x-a2a-hop"];
  if (raw === undefined) return 0;
  const n = Number(Array.isArray(raw) ? raw[0] : raw);
  if (!Number.isFinite(n) || n < 0) return 0;
  return Math.floor(n);
}

// Review-18 P1-J1: SSRF parity. Mirrors the Python sidecar's iter-17
// A2A_ALLOW_CLIENT_REGISTRY_URL gate. Default off — a client cannot
// point /a2a/outbound's registry lookup at an arbitrary URL unless
// the operator opts in.
function readAllowClientRegistryUrl(env) {
  const raw = String(env.A2A_ALLOW_CLIENT_REGISTRY_URL || "")
    .trim()
    .toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes" || raw === "on";
}

// Review-2 P1-D: request correlation.
//
// A single outbound-initiated hop chain (A→B→C) should share one stable ID
// so operators can grep sidecar logs across containers for a federation
// call. Accept a caller-supplied X-A2A-Request-Id (higher layers may
// pre-tag) and mint a fresh uuid4 when absent. The ID is logged at entry
// + exit, forwarded to the next hop, and echoed in the JSON body AND the
// response header so both JSON-parsing clients and curl-pipe-grep users
// can recover it.
const REQUEST_ID_HEADER = "x-a2a-request-id";

function looksLikeRequestId(value) {
  if (typeof value !== "string") return false;
  if (!value || value.length > 128) return false;
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code < 0x20) return false;
    if (code === 0x20 || code === 0x09 || code === 0x0a || code === 0x0d) return false;
  }
  return true;
}

function readOrMintRequestId(req) {
  const raw = req.headers[REQUEST_ID_HEADER];
  const candidate = Array.isArray(raw) ? raw[0] : raw;
  if (looksLikeRequestId(typeof candidate === "string" ? candidate.trim() : "")) {
    return candidate.trim();
  }
  // uuid4 without dashes keeps the log format tight; full uuid is still
  // accepted when minted elsewhere.
  return crypto.randomUUID().replace(/-/g, "");
}

async function postChatCompletion({
  gatewayHost,
  gatewayPort,
  token,
  userMessage,
  systemPrompt,
  history = [],
  model,
  timeoutMs,
}) {
  const payload = {
    model: model || "openclaw",
    stream: false,
    messages: [
      ...(systemPrompt ? [{ role: "system", content: systemPrompt }] : []),
      ...history,
      { role: "user", content: userMessage },
    ],
  };
  const headers = token ? { authorization: `Bearer ${token}` } : {};
  const { status, body } = await postJson({
    host: gatewayHost,
    port: gatewayPort,
    path: "/v1/chat/completions",
    headers,
    bodyObj: payload,
    timeoutMs,
  });
  if (status !== 200) {
    throw new Error(`gateway /v1/chat/completions ${status}: ${body.slice(0, 400)}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch (e) {
    throw new Error(`gateway returned non-json: ${e.message}`);
  }
  const choice = Array.isArray(parsed.choices) ? parsed.choices[0] : null;
  const content = choice?.message?.content;
  if (typeof content !== "string" || !content) {
    throw new Error(`gateway returned empty content: ${body.slice(0, 400)}`);
  }
  return content;
}

function jsonResponse(res, status, body, extraHeaders) {
  const payload = JSON.stringify(body);
  const headers = {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  };
  if (extraHeaders) {
    for (const [name, value] of Object.entries(extraHeaders)) {
      headers[name] = value;
    }
  }
  res.writeHead(status, headers);
  res.end(payload);
}

function readJsonBody(req, limit = 64 * 1024) {
  return new Promise((resolve, reject) => {
    let raw = "";
    let tooBig = false;
    req.on("data", (chunk) => {
      if (tooBig) return;
      raw += chunk;
      if (raw.length > limit) {
        tooBig = true;
        req.destroy();
        reject(new Error("request body too large"));
        return;
      }
    });
    req.on("end", () => {
      if (tooBig) return;
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(new Error(`invalid json: ${e.message}`));
      }
    });
    req.on("error", reject);
  });
}

// The system-prompt hint lets the agent know it's being invoked from a peer
// over A2A rather than from its primary user. The agent's own persona
// (IDENTITY.md / pre-registered skills) still drives tone and capabilities.
function buildA2AContext(selfName, fromAgent) {
  return (
    `You are being addressed by a peer agent named "${fromAgent}" ` +
    `over the A2A bridge as "${selfName}". Respond in plain text, ` +
    `preserving your own persona and skills. Keep the reply focused on ` +
    `the peer's request; do not prefix with your own name.`
  );
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const local = Boolean(args.local);

  let adapter;
  let instance;
  if (local) {
    instance = args.instance || args.name || process.env.A2A_SIDECAR_NAME || os.hostname();
    adapter = makeLocalAdapter();
  } else {
    instance = args.instance;
    if (!instance) {
      console.error("usage: node server.js --instance <name> --port <port> [--container <name>]");
      console.error("   or: node server.js --local --port <port> [--name <name>]");
      process.exit(64);
    }
    const container = args.container || `clawcu-openclaw-${instance}`;
    adapter = makeHostAdapter(container);
  }

  const port = Number(args.port || process.env.A2A_SIDECAR_PORT);
  if (!Number.isFinite(port) || port <= 0) {
    console.error("missing or invalid --port (or A2A_SIDECAR_PORT env)");
    process.exit(64);
  }

  const selfName = args.name || process.env.A2A_SIDECAR_NAME || instance;
  const defaultRole = local
    ? `OpenClaw agent "${selfName}"`
    : `OpenClaw agent "${selfName}" (sidecar-bridged)`;
  const role = args.role || process.env.A2A_SIDECAR_ROLE || defaultRole;
  const rawSkills = args.skills || process.env.A2A_SIDECAR_SKILLS || "chat,reason";
  const skills = rawSkills.split(",").map((s) => s.trim()).filter(Boolean);

  const bindHost = local ? "0.0.0.0" : "127.0.0.1";
  const advertiseHost =
    args["advertise-host"] || process.env.A2A_SIDECAR_ADVERTISE_HOST || "127.0.0.1";
  const advertisePort = Number(
    args["advertise-port"] || process.env.A2A_SIDECAR_ADVERTISE_PORT || port
  );
  const endpoint = `http://${advertiseHost}:${advertisePort}/a2a/send`;

  // Where the gateway's OpenAI-compat endpoint is reachable from the
  // sidecar. In baked-image (local) mode the gateway is on loopback in
  // the same netns; in host mode we would have to `docker exec curl`
  // which is not worth it — host mode is kept for one-off debugging.
  const gatewayHost = local
    ? "127.0.0.1"
    : args["gateway-host"] || process.env.A2A_GATEWAY_HOST || "127.0.0.1";
  const gatewayPort = Number(
    args["gateway-port"] || process.env.A2A_GATEWAY_PORT || process.env.OPENCLAW_GATEWAY_PORT || 18789
  );
  const requestTimeoutMs = Number(
    args["request-timeout-ms"] || process.env.A2A_REQUEST_TIMEOUT_MS || 120000
  );
  const gatewayReadyDeadlineMs = Number(
    args["gateway-ready-deadline-ms"] ||
      process.env.A2A_GATEWAY_READY_DEADLINE_MS ||
      30000
  );
  // Review-7 P2-E: the sidecar is gateway-agnostic; the adapter tells us
  // which path to probe. Default keeps existing behavior for openclaw.
  const gatewayReadyPathRaw =
    args["gateway-ready-path"] || process.env.A2A_GATEWAY_READY_PATH || "/healthz";
  const gatewayReadyPath = gatewayReadyPathRaw.startsWith("/")
    ? gatewayReadyPathRaw
    : `/${gatewayReadyPathRaw}`;
  const model = args.model || process.env.A2A_MODEL || "openclaw";
  // Review-9 P2-A: per-peer rate limit. 0 disables; default 30/min is
  // conservative enough that humans pair-chatting won't hit it, but a
  // runaway loop gets throttled.
  const rateLimitPerMinute = Number(
    args["rate-limit-per-minute"] ||
      process.env.A2A_RATE_LIMIT_PER_MINUTE ||
      30,
  );
  const rateLimiter = createRateLimiter({ perMinute: rateLimitPerMinute });
  // Review-13 P1-C: optional thread-history store. Disabled unless the
  // adapter sets A2A_THREAD_DIR (points at a datadir-mounted directory).
  const threadMaxPairs = Number(
    process.env.A2A_THREAD_MAX_HISTORY_PAIRS || 10,
  );
  const threadStore = createThreadStore({
    storageDir: process.env.A2A_THREAD_DIR || "",
    maxHistoryPairs: Number.isFinite(threadMaxPairs) && threadMaxPairs >= 0
      ? threadMaxPairs
      : 10,
  });

  const card = { name: selfName, role, skills, endpoint };

  // Lazy-init'd on first /mcp request. See handler body for why.
  let peerCache = null;

  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, `http://${req.headers.host}`);
      if (req.method === "GET" && url.pathname === "/.well-known/agent-card.json") {
        return jsonResponse(res, 200, card);
      }
      if (
        req.method === "GET" &&
        (url.pathname === "/health" || url.pathname === "/healthz")
      ) {
        // Accept both spellings: the sidecar hides the gateway's choice
        // (openclaw uses /healthz, hermes uses /health) from external
        // callers so monitoring that aims at either works. Review-7 P2-E.
        return jsonResponse(res, 200, {
          ok: true,
          instance: selfName,
          plugin_version: process.env.CLAWCU_PLUGIN_VERSION || "unknown",
          mode: "native-agent",
          gateway: `${gatewayHost}:${gatewayPort}`,
        });
      }
      if (req.method === "POST" && url.pathname === "/a2a/send") {
        const incomingHop = readHopHeader(req);
        const requestId = readOrMintRequestId(req);
        const ridHeaders = { [REQUEST_ID_HEADER]: requestId };
        if (incomingHop >= A2A_HOP_BUDGET) {
          console.warn(
            `[sidecar:${selfName}] a2a.send refused request_id=${requestId} hop=${incomingHop} budget=${A2A_HOP_BUDGET}`,
          );
          return jsonResponse(res, 508, {
            error: `hop budget exceeded (hop=${incomingHop}, budget=${A2A_HOP_BUDGET})`,
            request_id: requestId,
          }, ridHeaders);
        }
        let body;
        try {
          body = await readJsonBody(req);
        } catch (e) {
          return jsonResponse(res, 400, { error: e.message, request_id: requestId }, ridHeaders);
        }
        if (typeof body.message !== "string" || !body.message) {
          return jsonResponse(res, 400, { error: "missing 'message' (string)", request_id: requestId }, ridHeaders);
        }
        if (typeof body.from !== "string" || !body.from) {
          return jsonResponse(res, 400, { error: "missing 'from' (string)", request_id: requestId }, ridHeaders);
        }
        // Review-13 P1-C: thread_id is OPTIONAL. Peers that don't send it
        // behave exactly like before (stateless turns). When present, it
        // must be a clean string — we sanitize in thread.js, but reject
        // wrong types up here so callers get a 400 instead of silently
        // losing context.
        const threadId =
          typeof body.thread_id === "string" && body.thread_id
            ? body.thread_id
            : null;
        if (body.thread_id !== undefined && threadId === null) {
          return jsonResponse(res, 400, {
            error: "'thread_id' must be a non-empty string when provided",
            request_id: requestId,
          }, ridHeaders);
        }
        console.log(
          `[sidecar:${selfName}] a2a.send accepted request_id=${requestId} from=${body.from} hop=${incomingHop}`,
        );
        // Review-9 P2-A: check rate limit BEFORE auth/ready so a flood
        // doesn't even hit gateway-auth codepaths. 429 is retriable; we
        // advertise the reset window in the body so the peer can back off.
        const rl = rateLimiter.allow(body.from);
        if (!rl.ok) {
          res.setHeader("Retry-After", Math.ceil(rl.resetMs / 1000));
          return jsonResponse(res, 429, {
            error: `rate limit exceeded for peer '${body.from}'`,
            resetMs: rl.resetMs,
            request_id: requestId,
          }, ridHeaders);
        }
        let auth;
        try {
          auth = readGatewayAuth(adapter);
        } catch (e) {
          return jsonResponse(res, 503, { error: `instance not ready: ${e.message}`, request_id: requestId }, ridHeaders);
        }
        const ready = await waitForGatewayReady({
          host: gatewayHost,
          port: gatewayPort,
          path: gatewayReadyPath,
          deadlineMs: gatewayReadyDeadlineMs,
        });
        if (!ready) {
          return jsonResponse(res, 503, {
            error: `gateway not ready after ${gatewayReadyDeadlineMs}ms`,
            request_id: requestId,
          }, ridHeaders);
        }
        const history =
          threadId && threadStore.enabled
            ? threadStore.loadHistory(body.from, threadId)
            : [];
        let reply;
        try {
          reply = await postChatCompletion({
            gatewayHost,
            gatewayPort,
            token: auth.token,
            userMessage: body.message,
            systemPrompt: buildA2AContext(selfName, body.from),
            history,
            model,
            timeoutMs: requestTimeoutMs,
          });
        } catch (e) {
          console.error(`[sidecar:${selfName}] gateway call failed request_id=${requestId}:`, e.message);
          // 4xx are client-side (auth / model name); 5xx + socket errors
          // suggest the gateway died mid-flight. Cache is dropped only on
          // the latter, so next request re-probes. Regex list lives in
          // readiness.js::GATEWAY_DOWN_PATTERNS for testability.
          if (looksLikeGatewayDown(e)) invalidateGatewayReady();
          return jsonResponse(res, 502, { error: `upstream agent failed: ${e.message}`, request_id: requestId }, ridHeaders);
        }
        if (threadId && threadStore.enabled) {
          threadStore.appendTurn(body.from, threadId, body.message, reply);
        }
        console.log(
          `[sidecar:${selfName}] a2a.send replied request_id=${requestId} from=${body.from}`,
        );
        return jsonResponse(res, 200, {
          from: selfName,
          reply,
          // Echo so the peer can confirm the thread it landed in (and so a
          // CLI tail can correlate). Null when the peer didn't send one.
          thread_id: threadId,
          request_id: requestId,
        }, ridHeaders);
      }
      if (req.method === "POST" && url.pathname === "/a2a/outbound") {
        // Container-local outbound primitive (see a2a-design-1.md §Protocol).
        // Caller is inside the same netns; no auth is enforced because the
        // socket binds 127.0.0.1. Body: {to, message, thread_id?, registry_url?,
        // timeout_ms?}. Returns {from, to, reply, thread_id, request_id}.
        const incomingHop = readHopHeader(req);
        const requestId = readOrMintRequestId(req);
        const ridHeaders = { [REQUEST_ID_HEADER]: requestId };
        if (incomingHop >= A2A_HOP_BUDGET) {
          console.warn(
            `[sidecar:${selfName}] a2a.outbound refused request_id=${requestId} hop=${incomingHop} budget=${A2A_HOP_BUDGET}`,
          );
          return jsonResponse(res, 508, {
            error: `hop budget exceeded (hop=${incomingHop}, budget=${A2A_HOP_BUDGET})`,
            request_id: requestId,
          }, ridHeaders);
        }
        let body;
        try {
          body = await readJsonBody(req);
        } catch (e) {
          return jsonResponse(res, 400, { error: e.message, request_id: requestId }, ridHeaders);
        }
        if (typeof body.to !== "string" || !body.to) {
          return jsonResponse(res, 400, { error: "missing 'to' (string)", request_id: requestId }, ridHeaders);
        }
        if (typeof body.message !== "string" || !body.message) {
          return jsonResponse(res, 400, { error: "missing 'message' (string)", request_id: requestId }, ridHeaders);
        }
        const outThreadId =
          typeof body.thread_id === "string" && body.thread_id
            ? body.thread_id
            : null;
        if (body.thread_id !== undefined && outThreadId === null) {
          return jsonResponse(res, 400, {
            error: "'thread_id' must be a non-empty string when provided",
            request_id: requestId,
          }, ridHeaders);
        }
        // Self-origin rate limit (a2a-design-4.md §P1-B). One LLM turn
        // firing 200 a2a_call_peer calls doesn't nuke provider quota.
        const limitKey = outboundLimitKey({ threadId: outThreadId, selfName });
        const limit = OUTBOUND_LIMITER.check(limitKey);
        if (!limit.allowed) {
          console.warn(
            `[sidecar:${selfName}] a2a.outbound self-rate-limited request_id=${requestId} key=${limitKey} limit=${limit.limit}`,
          );
          return jsonResponse(
            res,
            429,
            {
              error: `self-origin rate limit exceeded (${limit.limit}/min)`,
              request_id: requestId,
              retry_after_ms: limit.retryAfterMs,
            },
            ridHeaders,
          );
        }
        // Review-18 P1-J1: gate the client-supplied registry_url override.
        // Without this, an attacker can point the sidecar at any http(s) URL
        // and either (a) exfil response body via the "registry lookup …"
        // error message, or (b) coerce forwardToPeer into POSTing the
        // outbound body to an attacker-chosen URL via a malicious card.
        let registryUrl;
        if (Object.prototype.hasOwnProperty.call(body, "registry_url")) {
          if (!readAllowClientRegistryUrl(process.env)) {
            return jsonResponse(
              res,
              400,
              {
                error: "client-supplied 'registry_url' is disabled by server policy",
                request_id: requestId,
              },
              ridHeaders,
            );
          }
          if (typeof body.registry_url !== "string" || !body.registry_url) {
            return jsonResponse(
              res,
              400,
              {
                error: "'registry_url' must be a non-empty string when provided",
                request_id: requestId,
              },
              ridHeaders,
            );
          }
          registryUrl = body.registry_url;
        } else {
          registryUrl =
            process.env.A2A_REGISTRY_URL || "http://host.docker.internal:9100";
        }
        const timeoutMs = Number.isFinite(Number(body.timeout_ms))
          ? Number(body.timeout_ms)
          : 60000;
        console.log(
          `[sidecar:${selfName}] a2a.outbound begin request_id=${requestId} to=${body.to} hop=${incomingHop}`,
        );
        let card;
        try {
          card = await lookupPeer({
            registryUrl,
            peerName: body.to,
            timeoutMs,
          });
        } catch (e) {
          const status = e.httpStatus || 503;
          console.warn(
            `[sidecar:${selfName}] a2a.outbound lookup-failed request_id=${requestId} to=${body.to} status=${status}`,
          );
          return jsonResponse(res, status, { error: e.message, request_id: requestId }, ridHeaders);
        }
        let peerResp;
        try {
          peerResp = await forwardToPeer({
            endpoint: card.endpoint,
            selfName,
            peerName: body.to,
            message: body.message,
            threadId: outThreadId,
            hop: incomingHop + 1,
            timeoutMs,
            requestId,
          });
        } catch (e) {
          const status = e.httpStatus || 502;
          console.warn(
            `[sidecar:${selfName}] a2a.outbound forward-failed request_id=${requestId} to=${body.to} status=${status} peer_status=${e.peerStatus ?? "-"}`,
          );
          const payload = { error: e.message, request_id: requestId };
          if (e.peerStatus !== undefined) payload.peer_status = e.peerStatus;
          return jsonResponse(res, status, payload, ridHeaders);
        }
        console.log(
          `[sidecar:${selfName}] a2a.outbound done request_id=${requestId} to=${body.to}`,
        );
        return jsonResponse(res, 200, {
          from: selfName,
          to: body.to,
          reply: typeof peerResp.reply === "string" ? peerResp.reply : "",
          thread_id:
            typeof peerResp.thread_id === "string"
              ? peerResp.thread_id
              : outThreadId,
          request_id: requestId,
        }, ridHeaders);
      }
      if (req.method === "POST" && url.pathname === "/mcp") {
        // MCP streamable-http. Shares request-id with /a2a/outbound so an
        // LLM→MCP→peer chain is one grep-able transaction.
        const requestId = readOrMintRequestId(req);
        const ridHeaders = { [REQUEST_ID_HEADER]: requestId };
        let rpc;
        try {
          rpc = await readJsonBody(req);
        } catch (e) {
          return jsonResponse(
            res,
            400,
            { jsonrpc: "2.0", id: null, error: { code: -32700, message: e.message } },
            ridHeaders,
          );
        }
        const registryUrl =
          process.env.A2A_REGISTRY_URL || "http://host.docker.internal:9100";
        console.log(
          `[sidecar:${selfName}] mcp.request request_id=${requestId} method=${rpc && rpc.method}`,
        );
        // Lazy-init the peer cache on first /mcp request. The registry URL
        // doesn't change within a process lifetime in practice (adapter
        // pins it at container start), so caching by URL is safe.
        if (!peerCache) {
          peerCache = createPeerCache({ registryUrl, timeoutMs: 5000 });
        }
        const response = await handleMcpRequest({
          body: rpc,
          deps: {
            selfName,
            registryUrl,
            timeoutMs: 60000,
            requestId,
            pluginVersion: process.env.CLAWCU_PLUGIN_VERSION || "unknown",
            lookupPeer,
            forwardToPeer,
            outboundLimiter: OUTBOUND_LIMITER,
            outboundLimitKey,
            listPeers:
              process.env.A2A_TOOL_DESC_MODE === "static"
                ? null
                : () => peerCache.get(),
            // a2a-design-6.md §P1-M: opt-in role in peer summary.
            includeRole:
              String(process.env.A2A_TOOL_DESC_INCLUDE_ROLE || "").toLowerCase() === "true",
          },
        });
        return jsonResponse(res, 200, response, ridHeaders);
      }
      jsonResponse(res, 404, { error: "not found" });
    } catch (err) {
      console.error(`[sidecar:${selfName}] unhandled:`, err && err.stack ? err.stack : err);
      jsonResponse(res, 500, { error: "internal error" });
    }
  });

  try {
    runMcpBootstrap({
      env: {
        ...process.env,
        A2A_SIDECAR_PORT: String(port),
        A2A_SERVICE_MCP_CONFIG_PATH:
          process.env.A2A_SERVICE_MCP_CONFIG_PATH || OPENCLAW_CONFIG_PATH,
        A2A_SERVICE_MCP_CONFIG_FORMAT: process.env.A2A_SERVICE_MCP_CONFIG_FORMAT || "json",
      },
    });
  } catch (err) {
    console.warn(
      `[sidecar:${selfName}] mcp-bootstrap threw: ${err && err.message}; continuing`,
    );
  }

  // a2a-design-6.md §P2-L / a2a-design-7.md §P1-N: periodic empty-bucket
  // sweep so a long-lived sidecar with high thread_id churn doesn't
  // accumulate empty buckets. Wired here (not module scope) so require()
  // from a test file doesn't start a real interval. Opt out with
  // A2A_OUTBOUND_SWEEP_INTERVAL_MS=0; handle is .unref()'d so it never
  // keeps the event loop alive past a graceful shutdown.
  createOutboundSweepTimer({
    limiter: OUTBOUND_LIMITER,
    intervalMs: readOutboundSweepIntervalMs(process.env),
  });

  server.listen(port, bindHost, () => {
    console.log(
      `[sidecar:${selfName}] mode=${adapter.mode} listening on http://${bindHost}:${port} ` +
        `(endpoint=${endpoint}, gateway=${gatewayHost}:${gatewayPort})`
    );
    console.log(`  GET  /.well-known/agent-card.json`);
    console.log(`  POST /a2a/send      → gateway /v1/chat/completions (native agent)`);
    console.log(`  POST /a2a/outbound  → registry lookup → peer /a2a/send`);
    console.log(`  POST /mcp           → MCP streamable-http (tool: a2a_call_peer)`);
  });

  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      console.log(`[sidecar:${selfName}] ${sig}, closing...`);
      server.close(() => process.exit(0));
      setTimeout(() => process.exit(0), 2000).unref();
    });
  }
}

if (require.main === module) main();

// Exposed for node:test unit tests. Not part of the sidecar's public
// protocol surface.
module.exports = {
  lookupPeer,
  forwardToPeer,
  fetchPeerList,
  createPeerCache,
  readJsonBody,
  readHopHeader,
  parseHttpUrl,
  readOrMintRequestId,
  looksLikeRequestId,
  readAllowClientRegistryUrl,
  readCappedBody,
  A2A_HOP_BUDGET,
  A2A_MAX_RESPONSE_BYTES,
  REQUEST_ID_HEADER,
};
