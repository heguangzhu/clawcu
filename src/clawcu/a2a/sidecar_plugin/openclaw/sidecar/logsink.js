"use strict";

// File-tee for console.log / console.error in the openclaw sidecar.
// Review-10 P2-C: by default the sidecar only writes to stderr, which
// means the only way to retrieve old logs is `docker logs <container>` —
// and those get rotated / lost when the container is recreated
// (`clawcu recreate`). Routing a copy to <datadir>/logs/a2a-sidecar.log
// keeps them across recreates (the datadir is a host bind-mount).
//
// Design:
// - Opt-in via A2A_SIDECAR_LOG_DIR. If unset, we don't touch console.* —
//   existing behavior is preserved byte-for-byte.
// - We *tee*, not redirect. Stderr still sees every line so `docker logs`
//   and test harnesses (which spawn the sidecar and capture stderr) keep
//   working.
// - Append-only. No rotation — that's `logrotate`'s job, not ours.
//   Rotating inside a sidecar means coordinating multiple processes
//   writing to the same file on a bind mount, which is a good way to
//   corrupt the log at the exact moment an operator wants to read it.
// - Best-effort. If mkdir or open fails (read-only FS, no space, bad
//   path), we swallow the error and fall back to stderr-only. A sidecar
//   that can't write to its log file should NOT refuse to serve traffic.

const fs = require("node:fs");
const path = require("node:path");

function setupFileLog(logDir) {
  if (!logDir) return { installed: false, reason: "no log dir" };
  let stream;
  try {
    fs.mkdirSync(logDir, { recursive: true });
    const logPath = path.join(logDir, "a2a-sidecar.log");
    stream = fs.createWriteStream(logPath, { flags: "a" });
  } catch (e) {
    // Intentionally swallow — the sidecar must remain serving.
    process.stderr.write(
      `[sidecar] log-file setup failed, stderr-only: ${e.message}\n`,
    );
    return { installed: false, reason: e.message };
  }

  const formatArg = (a) => {
    if (typeof a === "string") return a;
    if (a instanceof Error) return a.stack || a.message;
    try {
      return JSON.stringify(a);
    } catch {
      return String(a);
    }
  };

  const teeWrite = (level, args) => {
    const ts = new Date().toISOString();
    const msg = args.map(formatArg).join(" ");
    try {
      stream.write(`${ts} ${level} ${msg}\n`);
    } catch {
      // Stream errors are best-effort; don't break the request path.
    }
  };

  const origLog = console.log.bind(console);
  const origErr = console.error.bind(console);
  console.log = (...args) => {
    teeWrite("INFO", args);
    origLog(...args);
  };
  console.error = (...args) => {
    teeWrite("ERROR", args);
    origErr(...args);
  };

  return { installed: true, stream, logPath: path.join(logDir, "a2a-sidecar.log") };
}

module.exports = { setupFileLog };
