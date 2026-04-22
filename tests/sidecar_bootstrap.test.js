"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  MCP_ENTRY_NAME,
  buildMcpUrl,
  planBootstrap,
  runBootstrap,
} = require("../src/clawcu/a2a/sidecar_plugin/openclaw/sidecar/bootstrap.js");

function mkTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "clawcu-bootstrap-"));
}

function captureLogger() {
  const rec = { log: [], warn: [] };
  return {
    rec,
    log: (m) => rec.log.push(m),
    warn: (m) => rec.warn.push(m),
    error: (m) => rec.warn.push(m),
  };
}

test("buildMcpUrl renders the 127.0.0.1/mcp form", () => {
  assert.equal(buildMcpUrl({ port: 9129 }), "http://127.0.0.1:9129/mcp");
});

test("planBootstrap merges a2a when enabled and absent", () => {
  const plan = planBootstrap({
    enabled: true,
    config: { other: 1 },
    url: "http://127.0.0.1:18790/mcp",
  });
  assert.equal(plan.action, "merge");
  assert.deepEqual(plan.config.mcp.servers[MCP_ENTRY_NAME], {
    url: "http://127.0.0.1:18790/mcp",
  });
  assert.equal(plan.config.other, 1);
});

test("planBootstrap noops when entry already equals desired", () => {
  const plan = planBootstrap({
    enabled: true,
    config: {
      mcp: { servers: { a2a: { url: "http://127.0.0.1:18790/mcp" } } },
    },
    url: "http://127.0.0.1:18790/mcp",
  });
  assert.equal(plan.action, "noop");
});

test("planBootstrap rewrites when url differs (port changed across recreate)", () => {
  const plan = planBootstrap({
    enabled: true,
    config: {
      mcp: { servers: { a2a: { url: "http://127.0.0.1:1111/mcp" } } },
    },
    url: "http://127.0.0.1:2222/mcp",
  });
  assert.equal(plan.action, "merge");
  assert.equal(plan.config.mcp.servers.a2a.url, "http://127.0.0.1:2222/mcp");
});

test("planBootstrap removes stale entry when disabled", () => {
  const plan = planBootstrap({
    enabled: false,
    config: {
      mcp: { servers: { a2a: { url: "http://127.0.0.1:18790/mcp" }, other: { url: "x" } } },
    },
  });
  assert.equal(plan.action, "remove");
  assert.equal(plan.config.mcp.servers.a2a, undefined);
  assert.deepEqual(plan.config.mcp.servers.other, { url: "x" });
});

test("planBootstrap noop when disabled and entry absent", () => {
  const plan = planBootstrap({
    enabled: false,
    config: { mcp: { servers: { other: { url: "x" } } } },
  });
  assert.equal(plan.action, "noop");
});

test("planBootstrap preserves sibling keys in mcp.servers", () => {
  const plan = planBootstrap({
    enabled: true,
    config: {
      mcp: { servers: { context7: { command: "uvx" } } },
    },
    url: "http://127.0.0.1:18790/mcp",
  });
  assert.equal(plan.config.mcp.servers.context7.command, "uvx");
  assert.equal(plan.config.mcp.servers.a2a.url, "http://127.0.0.1:18790/mcp");
});

test("planBootstrap tolerates missing mcp block", () => {
  const plan = planBootstrap({
    enabled: true,
    config: {},
    url: "http://127.0.0.1:18790/mcp",
  });
  assert.equal(plan.action, "merge");
  assert.deepEqual(plan.config.mcp.servers.a2a, { url: "http://127.0.0.1:18790/mcp" });
});

test("runBootstrap skips when A2A_SERVICE_MCP_CONFIG_PATH unset", () => {
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({ env: { A2A_ENABLED: "true" }, logger });
  assert.equal(r.ok, true);
  assert.equal(r.action, "skip");
  assert.equal(r.reason, "no-config-path");
  assert.match(rec.log[0], /A2A_SERVICE_MCP_CONFIG_PATH unset/);
});

test("runBootstrap skips when format is not json (Hermes YAML handled by Python side)", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.yaml");
  fs.writeFileSync(configPath, "mcp:\n  servers: {}\n");
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_SERVICE_MCP_CONFIG_FORMAT: "yaml",
      A2A_ENABLED: "true",
      A2A_SIDECAR_PORT: "18790",
    },
    logger,
  });
  assert.equal(r.action, "skip");
  assert.equal(r.reason, "unsupported-format");
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap creates the file when enabled and config is absent", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "nested", "config.json");
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_ENABLED: "true",
      A2A_SIDECAR_PORT: "18790",
    },
    logger,
  });
  assert.equal(r.ok, true);
  assert.equal(r.action, "create");
  const written = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  assert.equal(written.mcp.servers.a2a.url, "http://127.0.0.1:18790/mcp");
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap no-ops on absent file when disabled", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.json");
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_ENABLED: "false",
    },
    logger,
  });
  assert.equal(r.action, "skip");
  assert.equal(r.reason, "file-absent-disabled");
  assert.equal(fs.existsSync(configPath), false);
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap merges into pre-existing config preserving other keys", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.json");
  const original = {
    gateway: { port: 18789 },
    mcp: { servers: { context7: { command: "uvx", args: ["context7-mcp"] } } },
  };
  fs.writeFileSync(configPath, JSON.stringify(original, null, 2));
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_ENABLED: "true",
      A2A_SIDECAR_PORT: "18790",
    },
    logger,
  });
  assert.equal(r.ok, true);
  assert.equal(r.action, "merge");
  const written = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  assert.equal(written.gateway.port, 18789);
  assert.equal(written.mcp.servers.context7.command, "uvx");
  assert.equal(written.mcp.servers.a2a.url, "http://127.0.0.1:18790/mcp");
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap removes stale a2a entry when disabled", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.json");
  const original = {
    mcp: { servers: { a2a: { url: "http://127.0.0.1:9999/mcp" }, keep: { url: "x" } } },
  };
  fs.writeFileSync(configPath, JSON.stringify(original));
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_ENABLED: "false",
    },
    logger,
  });
  assert.equal(r.action, "remove");
  const written = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  assert.equal(written.mcp.servers.a2a, undefined);
  assert.deepEqual(written.mcp.servers.keep, { url: "x" });
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap does not overwrite malformed JSON", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.json");
  fs.writeFileSync(configPath, "{not json");
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_ENABLED: "true",
      A2A_SIDECAR_PORT: "18790",
    },
    logger,
  });
  assert.equal(r.ok, false);
  assert.equal(fs.readFileSync(configPath, "utf-8"), "{not json");
  assert.ok(rec.warn.some((m) => /not valid JSON/.test(m)));
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap bails when enabled but port missing", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.json");
  fs.writeFileSync(configPath, "{}");
  const { rec, ...logger } = captureLogger();
  const r = runBootstrap({
    env: {
      A2A_SERVICE_MCP_CONFIG_PATH: configPath,
      A2A_ENABLED: "true",
    },
    logger,
  });
  assert.equal(r.action, "skip");
  assert.equal(r.reason, "no-port");
  assert.equal(fs.readFileSync(configPath, "utf-8"), "{}");
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("runBootstrap is idempotent across two invocations", () => {
  const tmp = mkTempDir();
  const configPath = path.join(tmp, "config.json");
  fs.writeFileSync(configPath, "{}");
  const { rec, ...logger } = captureLogger();
  const env = {
    A2A_SERVICE_MCP_CONFIG_PATH: configPath,
    A2A_ENABLED: "true",
    A2A_SIDECAR_PORT: "18790",
  };
  const first = runBootstrap({ env, logger });
  const second = runBootstrap({ env, logger });
  assert.equal(first.action, "merge");
  assert.equal(second.action, "noop");
  fs.rmSync(tmp, { recursive: true, force: true });
});
