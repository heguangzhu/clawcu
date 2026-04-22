"use strict";

// Minimal MCP server embedded in the A2A sidecar (a2a-design-3.md §P0-A).
//
// Transport: streamable-http. The caller POSTs a JSON-RPC 2.0 request to
// /mcp on the sidecar's existing port and gets a single JSON-RPC response
// back in the body. No SSE, no resumable sessions — streamable-http's
// "request/response" profile, which is enough for tool calls.
//
// Surface:
//   initialize → advertises protocolVersion + serverInfo + capabilities.tools
//   tools/list → returns [a2a_call_peer]
//   tools/call → name=a2a_call_peer → calls forwardToPeer in-process
//
// The MCP handler shares the same module-level helpers (lookupPeer,
// forwardToPeer) the /a2a/outbound HTTP handler uses, so an MCP tool call
// is a function call — not a second HTTP hop inside the container.

const MCP_PROTOCOL_VERSION = "2024-11-05";
const TOOL_NAME = "a2a_call_peer";
const MAX_PEERS_IN_DESCRIPTION = 16;
const MAX_SKILLS_IN_PEER_LINE = 3;

// Base description — shown on its own when no peer list is available, or as
// the prefix when a peer list is injected. Kept short on purpose; a long
// static description eats the LLM's context budget every turn.
const BASE_DESCRIPTION =
  "Call another agent in the A2A federation and return its reply. " +
  "Use when the current task needs data or work owned by a different " +
  "agent (e.g., an analyst for market data, a writer for prose).";

function formatPeerLine(peer, { includeRole = false } = {}) {
  const skills = Array.isArray(peer.skills) ? peer.skills : [];
  const head = skills.slice(0, MAX_SKILLS_IN_PEER_LINE).join(", ");
  const tail = skills.length > MAX_SKILLS_IN_PEER_LINE ? ", ..." : "";
  // a2a-design-6.md §P1-M: operator-gated role field. Default off so the
  // description stays short; set A2A_TOOL_DESC_INCLUDE_ROLE=true to enable.
  const role = includeRole && typeof peer.role === "string" && peer.role ? ` [${peer.role}]` : "";
  if (!head) return `  - ${peer.name}${role}`;
  return `  - ${peer.name}${role} (${head}${tail})`;
}

// Given the raw registry response and the caller's own name, produce the
// multi-line peer summary that gets appended to the tool description.
// Excludes self so the LLM doesn't try to call itself (wastes a hop and
// the hop-budget guard catches it anyway, but skipping is cleaner).
function formatPeerSummary(peers, selfName, { includeRole = false } = {}) {
  if (!Array.isArray(peers) || peers.length === 0) return "";
  const others = peers.filter((p) => p && p.name && p.name !== selfName);
  if (others.length === 0) return "";
  const shown = others
    .slice(0, MAX_PEERS_IN_DESCRIPTION)
    .map((p) => formatPeerLine(p, { includeRole }));
  const hiddenCount = others.length - shown.length;
  const tail = hiddenCount > 0 ? [`  ...and ${hiddenCount} more`] : [];
  return ["", "Available peers:", ...shown, ...tail].join("\n");
}

function toolDescriptor({ peers, selfName, includeRole = false } = {}) {
  let description = BASE_DESCRIPTION;
  const summary = formatPeerSummary(peers, selfName, { includeRole });
  if (summary) {
    description += summary;
    description +=
      "\n\nThe `to` field must match one of the peers above " +
      "(case-sensitive).";
  } else {
    description +=
      " The target agent name must be registered in the A2A registry.";
  }
  return {
    name: TOOL_NAME,
    description,
    inputSchema: {
      type: "object",
      properties: {
        to: {
          type: "string",
          description: "Peer agent name as registered in the A2A registry.",
        },
        message: {
          type: "string",
          description: "The question or task for the peer agent.",
        },
        thread_id: {
          type: "string",
          description:
            "Optional. Reuse a prior conversation thread with the peer.",
        },
      },
      required: ["to", "message"],
    },
  };
}

function jsonRpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function jsonRpcError(id, code, message, data) {
  const err = { code, message };
  if (data !== undefined) err.data = data;
  return { jsonrpc: "2.0", id, error: err };
}

// JSON-RPC 2.0 error codes (spec):
//   -32700 parse error, -32600 invalid request, -32601 method not found,
//   -32602 invalid params, -32603 internal error.
// We reserve -32000..-32099 for application errors (MCP convention).
const ERR_PARSE = -32700;
const ERR_INVALID_REQUEST = -32600;
const ERR_METHOD_NOT_FOUND = -32601;
const ERR_INVALID_PARAMS = -32602;
const ERR_INTERNAL = -32603;
const ERR_A2A_UPSTREAM = -32001;

async function handleMcpRequest({ body, deps }) {
  if (
    !body ||
    body.jsonrpc !== "2.0" ||
    typeof body.method !== "string"
  ) {
    return jsonRpcError(
      body && body.id !== undefined ? body.id : null,
      ERR_INVALID_REQUEST,
      "expected JSON-RPC 2.0 request",
    );
  }
  const { id = null, method, params = {} } = body;
  try {
    if (method === "initialize") {
      return jsonRpcResult(id, {
        protocolVersion: MCP_PROTOCOL_VERSION,
        serverInfo: {
          name: "clawcu-a2a",
          version: deps.pluginVersion || "unknown",
        },
        capabilities: { tools: {} },
      });
    }
    if (method === "tools/list") {
      let peers = null;
      if (typeof deps.listPeers === "function") {
        try {
          peers = await deps.listPeers();
        } catch (e) {
          // Never fail tools/list on a registry hiccup — the LLM must still
          // see the tool or it won't know to call it (a2a-design-5.md §P1-H).
          peers = null;
        }
      }
      return jsonRpcResult(id, {
        tools: [
          toolDescriptor({
            peers,
            selfName: deps.selfName,
            includeRole: deps.includeRole === true,
          }),
        ],
      });
    }
    if (method === "tools/call") {
      return await handleToolsCall(id, params, deps);
    }
    if (method === "notifications/initialized" || method === "ping") {
      return jsonRpcResult(id, {});
    }
    return jsonRpcError(id, ERR_METHOD_NOT_FOUND, `unknown method: ${method}`);
  } catch (e) {
    return jsonRpcError(id, ERR_INTERNAL, e && e.message ? e.message : String(e));
  }
}

async function handleToolsCall(id, params, deps) {
  // P2-K: every MCP error in this handler carries requestId in data so a
  // JSON-RPC-only client can correlate to X-A2A-Request-Id without parsing
  // headers.
  const withRid = (extra) => ({ ...(extra || {}), requestId: deps.requestId || null });
  if (!params || params.name !== TOOL_NAME) {
    return jsonRpcError(
      id,
      ERR_METHOD_NOT_FOUND,
      `unknown tool: ${params && params.name}`,
      withRid(),
    );
  }
  const args = params.arguments || {};
  if (typeof args.to !== "string" || !args.to) {
    return jsonRpcError(id, ERR_INVALID_PARAMS, "missing 'to' (string)", withRid());
  }
  if (typeof args.message !== "string" || !args.message) {
    return jsonRpcError(id, ERR_INVALID_PARAMS, "missing 'message' (string)", withRid());
  }
  const threadId =
    typeof args.thread_id === "string" && args.thread_id
      ? args.thread_id
      : null;

  // Self-origin rate limit (a2a-design-4.md §P1-B). Shared bucket with
  // /a2a/outbound so the LLM firing 200 a2a_call_peer calls in one turn
  // can't nuke the provider quota.
  if (deps.outboundLimiter && deps.outboundLimitKey) {
    const key = deps.outboundLimitKey({
      threadId,
      selfName: deps.selfName,
    });
    const decision = deps.outboundLimiter.check(key);
    if (!decision.allowed) {
      return jsonRpcError(
        id,
        ERR_A2A_UPSTREAM,
        `self-origin rate limit exceeded (${decision.limit}/min)`,
        withRid({
          httpStatus: 429,
          retryAfterMs: decision.retryAfterMs,
        }),
      );
    }
  }

  let card;
  try {
    card = await deps.lookupPeer({
      registryUrl: deps.registryUrl,
      peerName: args.to,
      timeoutMs: deps.timeoutMs,
    });
  } catch (e) {
    return jsonRpcError(
      id,
      ERR_A2A_UPSTREAM,
      `registry lookup failed: ${e.message}`,
      withRid({ httpStatus: e.httpStatus || 503 }),
    );
  }

  let peerResp;
  try {
    peerResp = await deps.forwardToPeer({
      endpoint: card.endpoint,
      selfName: deps.selfName,
      peerName: args.to,
      message: args.message,
      threadId,
      hop: 1,
      timeoutMs: deps.timeoutMs,
      requestId: deps.requestId,
    });
  } catch (e) {
    return jsonRpcError(id, ERR_A2A_UPSTREAM, `peer call failed: ${e.message}`, withRid({
      httpStatus: e.httpStatus || 502,
      peerStatus: e.peerStatus,
    }));
  }

  const reply = typeof peerResp.reply === "string" ? peerResp.reply : "";
  return jsonRpcResult(id, {
    content: [{ type: "text", text: reply }],
    isError: false,
    // Non-standard but harmless; callers who care about correlation can
    // read from the top-level response header instead.
    structuredContent: {
      from: peerResp.from || deps.selfName,
      to: args.to,
      reply,
      thread_id:
        typeof peerResp.thread_id === "string" ? peerResp.thread_id : threadId,
      request_id: deps.requestId || null,
    },
  });
}

module.exports = {
  handleMcpRequest,
  toolDescriptor,
  TOOL_NAME,
  MCP_PROTOCOL_VERSION,
  ERR_INVALID_REQUEST,
  ERR_METHOD_NOT_FOUND,
  ERR_INVALID_PARAMS,
  ERR_A2A_UPSTREAM,
};
