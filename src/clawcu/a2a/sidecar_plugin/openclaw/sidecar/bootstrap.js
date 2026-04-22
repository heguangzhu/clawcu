"use strict";

// Auto-wire the a2a MCP entry into the OpenClaw config file on sidecar
// start. Merges (A2A_ENABLED=true) or removes (otherwise) `mcp.servers.a2a`
// from the JSON config located at $A2A_SERVICE_MCP_CONFIG_PATH. Safe by
// construction: any failure (missing path env, unreadable file, parse
// error) logs a warning and returns without touching the file.
//
// The caller wires this in `server.js::main()` before `server.listen()` so
// the service reads the merged config on its first MCP-config load.

const fs = require("node:fs");
const path = require("node:path");

const MCP_ENTRY_NAME = "a2a";

function _deepGet(obj, keys) {
  let cur = obj;
  for (const k of keys) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = cur[k];
  }
  return cur;
}

function _ensureObject(parent, key) {
  if (parent[key] == null || typeof parent[key] !== "object" || Array.isArray(parent[key])) {
    parent[key] = {};
  }
  return parent[key];
}

function buildMcpUrl({ port }) {
  return `http://127.0.0.1:${port}/mcp`;
}

function planBootstrap({ enabled, config, url }) {
  const safeConfig = config && typeof config === "object" && !Array.isArray(config) ? config : {};
  const current = _deepGet(safeConfig, ["mcp", "servers", MCP_ENTRY_NAME]);

  if (enabled) {
    const desired = { url };
    const same =
      current &&
      typeof current === "object" &&
      !Array.isArray(current) &&
      current.url === desired.url &&
      Object.keys(current).length === 1;
    if (same) return { action: "noop", reason: "already-present", config: safeConfig };
    const next = JSON.parse(JSON.stringify(safeConfig));
    const mcp = _ensureObject(next, "mcp");
    const servers = _ensureObject(mcp, "servers");
    servers[MCP_ENTRY_NAME] = desired;
    return { action: "merge", config: next };
  }

  if (!current) return { action: "noop", reason: "absent", config: safeConfig };
  const next = JSON.parse(JSON.stringify(safeConfig));
  const servers = _deepGet(next, ["mcp", "servers"]);
  if (servers && typeof servers === "object") {
    delete servers[MCP_ENTRY_NAME];
  }
  return { action: "remove", config: next };
}

function _atomicWriteJson(filePath, obj) {
  const tmp = `${filePath}.a2a-bootstrap.${process.pid}.tmp`;
  const text = JSON.stringify(obj, null, 2) + "\n";
  fs.writeFileSync(tmp, text, { encoding: "utf-8" });
  fs.renameSync(tmp, filePath);
}

function runBootstrap({ env, logger = console }) {
  const e = env || process.env;
  const configPath = e.A2A_SERVICE_MCP_CONFIG_PATH;
  if (!configPath) {
    logger.log("[sidecar:bootstrap] A2A_SERVICE_MCP_CONFIG_PATH unset — skipping MCP auto-wire");
    return { ok: true, action: "skip", reason: "no-config-path" };
  }
  const format = (e.A2A_SERVICE_MCP_CONFIG_FORMAT || "json").toLowerCase();
  if (format !== "json") {
    logger.log(
      `[sidecar:bootstrap] unsupported config format "${format}" — skipping (Node bootstrap handles JSON only)`,
    );
    return { ok: true, action: "skip", reason: "unsupported-format" };
  }
  const enabled = String(e.A2A_ENABLED || "").toLowerCase() === "true";
  const port = Number(e.A2A_SIDECAR_PORT || e.A2A_BIND_PORT);
  if (enabled && !(Number.isFinite(port) && port > 0)) {
    logger.warn(
      "[sidecar:bootstrap] A2A_ENABLED=true but sidecar port is unknown — skipping MCP auto-wire",
    );
    return { ok: true, action: "skip", reason: "no-port" };
  }
  const url = enabled ? buildMcpUrl({ port }) : null;

  let text;
  try {
    text = fs.readFileSync(configPath, "utf-8");
  } catch (err) {
    if (err && err.code === "ENOENT") {
      if (!enabled) {
        return { ok: true, action: "skip", reason: "file-absent-disabled" };
      }
      const next = { mcp: { servers: { [MCP_ENTRY_NAME]: { url } } } };
      try {
        fs.mkdirSync(path.dirname(configPath), { recursive: true });
        _atomicWriteJson(configPath, next);
        logger.log(`[sidecar:bootstrap] created ${configPath} with a2a MCP entry → ${url}`);
        return { ok: true, action: "create", path: configPath };
      } catch (writeErr) {
        logger.warn(`[sidecar:bootstrap] failed to create ${configPath}: ${writeErr.message}`);
        return { ok: false, action: "error", error: writeErr.message };
      }
    }
    logger.warn(`[sidecar:bootstrap] cannot read ${configPath}: ${err.message}`);
    return { ok: false, action: "error", error: err.message };
  }

  let config;
  try {
    config = text.trim() === "" ? {} : JSON.parse(text);
  } catch (err) {
    logger.warn(
      `[sidecar:bootstrap] ${configPath} is not valid JSON — refusing to overwrite (${err.message})`,
    );
    return { ok: false, action: "error", error: err.message };
  }

  const plan = planBootstrap({ enabled, config, url });
  if (plan.action === "noop") {
    logger.log(`[sidecar:bootstrap] ${configPath} already in desired state (${plan.reason})`);
    return { ok: true, action: "noop", reason: plan.reason };
  }

  try {
    _atomicWriteJson(configPath, plan.config);
    logger.log(
      `[sidecar:bootstrap] ${plan.action} a2a MCP entry in ${configPath}` +
        (url ? ` → ${url}` : ""),
    );
    return { ok: true, action: plan.action, path: configPath };
  } catch (err) {
    logger.warn(`[sidecar:bootstrap] write failed for ${configPath}: ${err.message}`);
    return { ok: false, action: "error", error: err.message };
  }
}

module.exports = {
  MCP_ENTRY_NAME,
  buildMcpUrl,
  planBootstrap,
  runBootstrap,
};
