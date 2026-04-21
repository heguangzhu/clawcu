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
        let raw = "";
        res.on("data", (chunk) => {
          raw += chunk;
        });
        res.on("end", () => {
          resolve({ status: res.statusCode ?? 0, body: raw });
        });
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

function jsonResponse(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
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
        reject(new Error("request body too large"));
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
        let body;
        try {
          body = await readJsonBody(req);
        } catch (e) {
          return jsonResponse(res, 400, { error: e.message });
        }
        if (typeof body.message !== "string" || !body.message) {
          return jsonResponse(res, 400, { error: "missing 'message' (string)" });
        }
        if (typeof body.from !== "string" || !body.from) {
          return jsonResponse(res, 400, { error: "missing 'from' (string)" });
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
          });
        }
        // Review-9 P2-A: check rate limit BEFORE auth/ready so a flood
        // doesn't even hit gateway-auth codepaths. 429 is retriable; we
        // advertise the reset window in the body so the peer can back off.
        const rl = rateLimiter.allow(body.from);
        if (!rl.ok) {
          res.setHeader("Retry-After", Math.ceil(rl.resetMs / 1000));
          return jsonResponse(res, 429, {
            error: `rate limit exceeded for peer '${body.from}'`,
            resetMs: rl.resetMs,
          });
        }
        let auth;
        try {
          auth = readGatewayAuth(adapter);
        } catch (e) {
          return jsonResponse(res, 503, { error: `instance not ready: ${e.message}` });
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
          });
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
          console.error(`[sidecar:${selfName}] gateway call failed:`, e.message);
          // 4xx are client-side (auth / model name); 5xx + socket errors
          // suggest the gateway died mid-flight. Cache is dropped only on
          // the latter, so next request re-probes. Regex list lives in
          // readiness.js::GATEWAY_DOWN_PATTERNS for testability.
          if (looksLikeGatewayDown(e)) invalidateGatewayReady();
          return jsonResponse(res, 502, { error: `upstream agent failed: ${e.message}` });
        }
        if (threadId && threadStore.enabled) {
          threadStore.appendTurn(body.from, threadId, body.message, reply);
        }
        return jsonResponse(res, 200, {
          from: selfName,
          reply,
          // Echo so the peer can confirm the thread it landed in (and so a
          // CLI tail can correlate). Null when the peer didn't send one.
          thread_id: threadId,
        });
      }
      jsonResponse(res, 404, { error: "not found" });
    } catch (err) {
      console.error(`[sidecar:${selfName}] unhandled:`, err && err.stack ? err.stack : err);
      jsonResponse(res, 500, { error: "internal error" });
    }
  });

  server.listen(port, bindHost, () => {
    console.log(
      `[sidecar:${selfName}] mode=${adapter.mode} listening on http://${bindHost}:${port} ` +
        `(endpoint=${endpoint}, gateway=${gatewayHost}:${gatewayPort})`
    );
    console.log(`  GET  /.well-known/agent-card.json`);
    console.log(`  POST /a2a/send  → gateway /v1/chat/completions (native agent)`);
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
