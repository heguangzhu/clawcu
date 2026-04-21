"use strict";

// Per-peer / per-thread conversation history for the OpenClaw A2A sidecar.
// Review-12 P1-C (local extension): A2A v0.1 has no `thread_id` in its
// payload yet, so conversation state between peer agents is lost across
// /a2a/send turns. This module lets a peer opt-in by sending an optional
// `thread_id` field; if present, the sidecar loads prior turns from disk
// and prepends them to /v1/chat/completions.messages so the native agent
// sees continuous context.
//
// Design choices:
//
// - **Initiator-generated id, echoed back**: the peer generates a UUID
//   (v7 recommended) on first turn and re-uses it. The sidecar does not
//   mint IDs — that would require a round-trip the peer doesn't need.
//
// - **Composite key `(peer, thread_id)`**: two peers could independently
//   generate the same UUID (astronomically unlikely, but cheaper to
//   namespace than to argue about). The on-disk layout
//   `<storageDir>/<peer>/<threadId>.jsonl` enforces this for free.
//
// - **append-only JSONL**: each turn appends two lines
//   `{role:"user"|"assistant", content, ts}`. Survives crashes, trivially
//   greppable, no locks needed (single-writer per sidecar process).
//
// - **maxHistoryPairs cap on LOAD, not on WRITE**: the file keeps every
//   turn (useful for audit / later summarization); we cap how many we
//   replay to the LLM to bound token cost. 10 pairs (= 20 messages) is
//   the default; operators can raise via A2A_THREAD_MAX_HISTORY_PAIRS.
//
// - **Path-traversal hardening**: peer and threadId must match a strict
//   charset; anything else → no-op load/append. A malicious peer cannot
//   write outside the storageDir.
//
// - **disabled when storageDir is empty**: same "one env flips it on"
//   posture as A2A_SIDECAR_LOG_DIR. Unset → sidecar behaves exactly as
//   before this module existed.

const fs = require("node:fs");
const path = require("node:path");

const SAFE_ID = /^[A-Za-z0-9._-]{1,128}$/;

function safeId(value) {
  if (typeof value !== "string" || !value) return null;
  if (!SAFE_ID.test(value)) return null;
  // Reject "." / ".." even though the regex technically allows them.
  if (value === "." || value === "..") return null;
  return value;
}

function createThreadStore({
  storageDir = "",
  maxHistoryPairs = 10,
  nowFn = () => new Date().toISOString(),
  fsModule = fs,
} = {}) {
  const enabled = Boolean(storageDir);

  function threadFilePath(peer, threadId) {
    const p = safeId(peer);
    const t = safeId(threadId);
    if (!p || !t) return null;
    const dir = path.join(storageDir, p);
    return { dir, file: path.join(dir, `${t}.jsonl`) };
  }

  function loadHistory(peer, threadId) {
    if (!enabled) return [];
    const paths = threadFilePath(peer, threadId);
    if (!paths) return [];
    let raw;
    try {
      raw = fsModule.readFileSync(paths.file, "utf8");
    } catch (e) {
      if (e && e.code === "ENOENT") return [];
      process.stderr.write(
        `a2a-sidecar: thread load failed for ${peer}/${threadId}: ${e.message}\n`,
      );
      return [];
    }
    const out = [];
    for (const line of raw.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let parsed;
      try {
        parsed = JSON.parse(trimmed);
      } catch {
        // Corrupt line — skip but keep going so one bad append doesn't
        // poison the whole thread.
        continue;
      }
      if (
        !parsed ||
        typeof parsed.content !== "string" ||
        (parsed.role !== "user" && parsed.role !== "assistant")
      ) {
        continue;
      }
      out.push({ role: parsed.role, content: parsed.content });
    }
    // Cap at the tail so long threads don't blow the context window.
    const cap = Math.max(0, maxHistoryPairs) * 2;
    if (cap > 0 && out.length > cap) {
      return out.slice(out.length - cap);
    }
    return out;
  }

  function appendTurn(peer, threadId, userMsg, assistantMsg) {
    if (!enabled) return false;
    const paths = threadFilePath(peer, threadId);
    if (!paths) return false;
    if (typeof userMsg !== "string" || typeof assistantMsg !== "string") {
      return false;
    }
    try {
      fsModule.mkdirSync(paths.dir, { recursive: true });
      const ts = nowFn();
      const lines =
        JSON.stringify({ role: "user", content: userMsg, ts }) +
        "\n" +
        JSON.stringify({ role: "assistant", content: assistantMsg, ts }) +
        "\n";
      fsModule.appendFileSync(paths.file, lines, "utf8");
      return true;
    } catch (e) {
      process.stderr.write(
        `a2a-sidecar: thread append failed for ${peer}/${threadId}: ${e.message}\n`,
      );
      return false;
    }
  }

  return {
    enabled,
    loadHistory,
    appendTurn,
  };
}

module.exports = { createThreadStore, safeId };
