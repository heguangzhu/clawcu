"use strict";

// Unit tests for the sidecar's MCP handler (a2a-design-3.md §P0-A).
//
// Scope:
//   - initialize / tools/list / tools/call method dispatch.
//   - tools/call invokes the injected lookupPeer + forwardToPeer helpers.
//   - Error shapes for missing args, unknown method, unknown tool, upstream
//     failures (registry lookup, peer forward).
//
// End-to-end (binding a port, POSTing /mcp through the real HTTP server)
// lives in tests/test_a2a.py, which already knows how to stand the full
// sidecar up.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const mcp = require(
  path.resolve(
    __dirname,
    "..",
    "src",
    "clawcu",
    "a2a",
    "sidecar_plugin",
    "openclaw",
    "sidecar",
    "mcp.js",
  ),
);

function baseDeps(overrides = {}) {
  return {
    selfName: "writer",
    registryUrl: "http://127.0.0.1:9100",
    timeoutMs: 2000,
    requestId: "req-1",
    pluginVersion: "0.3.3.testsha",
    lookupPeer: async () => ({
      name: "analyst",
      endpoint: "http://127.0.0.1:9129/a2a/send",
    }),
    forwardToPeer: async () => ({
      from: "analyst",
      reply: "42",
      thread_id: null,
    }),
    ...overrides,
  };
}

// -- initialize --------------------------------------------------------------

test("mcp: initialize returns protocolVersion + serverInfo", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 1, method: "initialize" },
    deps: baseDeps(),
  });
  assert.equal(res.jsonrpc, "2.0");
  assert.equal(res.id, 1);
  assert.equal(res.result.protocolVersion, mcp.MCP_PROTOCOL_VERSION);
  assert.equal(res.result.serverInfo.name, "clawcu-a2a");
  assert.equal(res.result.serverInfo.version, "0.3.3.testsha");
  assert.ok(res.result.capabilities.tools, "advertises tools capability");
});

// -- tools/list --------------------------------------------------------------

test("mcp: tools/list exposes a2a_call_peer only", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 2, method: "tools/list" },
    deps: baseDeps(),
  });
  assert.equal(res.result.tools.length, 1);
  const tool = res.result.tools[0];
  assert.equal(tool.name, "a2a_call_peer");
  assert.equal(tool.inputSchema.type, "object");
  assert.deepEqual(tool.inputSchema.required, ["to", "message"]);
  assert.ok("thread_id" in tool.inputSchema.properties);
});

// -- tools/call happy path ---------------------------------------------------

test("mcp: tools/call a2a_call_peer forwards to peer, returns text content", async () => {
  let seenLookup = null;
  let seenForward = null;
  const deps = baseDeps({
    lookupPeer: async (args) => {
      seenLookup = args;
      return { name: "analyst", endpoint: "http://127.0.0.1:9129/a2a/send" };
    },
    forwardToPeer: async (args) => {
      seenForward = args;
      return { from: "analyst", reply: "Q1 was up 18%", thread_id: "t-1" };
    },
  });
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: {
        name: "a2a_call_peer",
        arguments: { to: "analyst", message: "Q1 revenue?", thread_id: "t-1" },
      },
    },
    deps,
  });
  assert.equal(res.error, undefined, "no error");
  assert.equal(res.result.isError, false);
  assert.deepEqual(res.result.content, [
    { type: "text", text: "Q1 was up 18%" },
  ]);
  assert.equal(res.result.structuredContent.to, "analyst");
  assert.equal(res.result.structuredContent.reply, "Q1 was up 18%");
  assert.equal(res.result.structuredContent.thread_id, "t-1");
  assert.equal(res.result.structuredContent.request_id, "req-1");
  assert.equal(seenLookup.peerName, "analyst");
  assert.equal(seenLookup.registryUrl, "http://127.0.0.1:9100");
  assert.equal(seenForward.endpoint, "http://127.0.0.1:9129/a2a/send");
  assert.equal(seenForward.threadId, "t-1");
  assert.equal(seenForward.hop, 1);
  assert.equal(seenForward.requestId, "req-1");
});

test("mcp: tools/call without thread_id passes null through", async () => {
  let seenThread = "uninitialized";
  const deps = baseDeps({
    forwardToPeer: async (args) => {
      seenThread = args.threadId;
      return { from: "analyst", reply: "ok", thread_id: null };
    },
  });
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: { name: "a2a_call_peer", arguments: { to: "p", message: "m" } },
    },
    deps,
  });
  assert.equal(seenThread, null);
  assert.equal(res.result.structuredContent.thread_id, null);
});

// -- tools/call error paths --------------------------------------------------

test("mcp: tools/call missing 'to' → -32602 invalid params", async () => {
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 5,
      method: "tools/call",
      params: { name: "a2a_call_peer", arguments: { message: "hi" } },
    },
    deps: baseDeps(),
  });
  assert.equal(res.error.code, mcp.ERR_INVALID_PARAMS);
  assert.ok(/missing 'to'/.test(res.error.message));
});

test("mcp: tools/call missing 'message' → -32602", async () => {
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 6,
      method: "tools/call",
      params: { name: "a2a_call_peer", arguments: { to: "analyst" } },
    },
    deps: baseDeps(),
  });
  assert.equal(res.error.code, mcp.ERR_INVALID_PARAMS);
});

test("mcp: tools/call unknown tool name → -32601 method not found", async () => {
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 7,
      method: "tools/call",
      params: { name: "nonsense", arguments: {} },
    },
    deps: baseDeps(),
  });
  assert.equal(res.error.code, mcp.ERR_METHOD_NOT_FOUND);
});

test("mcp: tools/call registry lookup failure → -32001 with httpStatus 404", async () => {
  const deps = baseDeps({
    lookupPeer: async () => {
      const err = new Error("peer 'analyst' not found in registry");
      err.httpStatus = 404;
      throw err;
    },
  });
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 8,
      method: "tools/call",
      params: {
        name: "a2a_call_peer",
        arguments: { to: "analyst", message: "hi" },
      },
    },
    deps,
  });
  assert.equal(res.error.code, mcp.ERR_A2A_UPSTREAM);
  assert.equal(res.error.data.httpStatus, 404);
  assert.ok(/registry lookup failed/.test(res.error.message));
});

test("mcp: tools/call peer forward failure surfaces httpStatus + peerStatus", async () => {
  const deps = baseDeps({
    forwardToPeer: async () => {
      const err = new Error("peer HTTP 500");
      err.httpStatus = 502;
      err.peerStatus = 500;
      throw err;
    },
  });
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 9,
      method: "tools/call",
      params: {
        name: "a2a_call_peer",
        arguments: { to: "analyst", message: "hi" },
      },
    },
    deps,
  });
  assert.equal(res.error.code, mcp.ERR_A2A_UPSTREAM);
  assert.equal(res.error.data.httpStatus, 502);
  assert.equal(res.error.data.peerStatus, 500);
});

// -- top-level dispatch ------------------------------------------------------

test("mcp: unknown method → -32601", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 10, method: "resources/list" },
    deps: baseDeps(),
  });
  assert.equal(res.error.code, mcp.ERR_METHOD_NOT_FOUND);
});

test("mcp: non-JSON-RPC request → -32600 invalid request", async () => {
  const res = await mcp.handleMcpRequest({
    body: { method: "initialize" },
    deps: baseDeps(),
  });
  assert.equal(res.error.code, mcp.ERR_INVALID_REQUEST);
});

test("mcp: ping is acknowledged as empty result", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 11, method: "ping" },
    deps: baseDeps(),
  });
  assert.equal(res.error, undefined);
  assert.deepEqual(res.result, {});
});

test("mcp: notifications/initialized acknowledged", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: null, method: "notifications/initialized" },
    deps: baseDeps(),
  });
  assert.equal(res.error, undefined);
});

test("mcp: null id preserved in error response", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "1.0" },
    deps: baseDeps(),
  });
  assert.equal(res.id, null);
  assert.equal(res.error.code, mcp.ERR_INVALID_REQUEST);
});

// -- self-origin outbound rate limit (a2a-design-4.md §P1-B) -----------------

test("mcp: tools/call rate-limits after RPM and returns httpStatus=429 + retryAfterMs", async () => {
  const limit = require(
    path.resolve(
      __dirname,
      "..",
      "src",
      "clawcu",
      "a2a",
      "sidecar_plugin",
      "openclaw",
      "sidecar",
      "outbound_limit.js",
    ),
  );
  const limiter = limit.createOutboundLimiter({ rpm: 2 });

  async function callOnce() {
    return mcp.handleMcpRequest({
      body: {
        jsonrpc: "2.0",
        id: 99,
        method: "tools/call",
        params: {
          name: "a2a_call_peer",
          arguments: { to: "analyst", message: "hi", thread_id: "t-lim" },
        },
      },
      deps: baseDeps({
        outboundLimiter: limiter,
        outboundLimitKey: limit.keyFor,
      }),
    });
  }

  const r1 = await callOnce();
  const r2 = await callOnce();
  const r3 = await callOnce();

  assert.ok(r1.result, "first call under limit");
  assert.ok(r2.result, "second call under limit");
  assert.ok(r3.error, "third call rate-limited");
  assert.equal(r3.error.code, mcp.ERR_A2A_UPSTREAM);
  assert.equal(r3.error.data.httpStatus, 429);
  assert.ok(
    r3.error.data.retryAfterMs > 0,
    "retryAfterMs must be a positive hint",
  );
});

test("mcp: tools/call without limiter deps stays permissive", async () => {
  // Absence of limiter must be a no-op (deps.outboundLimiter undefined).
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 100,
      method: "tools/call",
      params: {
        name: "a2a_call_peer",
        arguments: { to: "analyst", message: "hi" },
      },
    },
    deps: baseDeps(),
  });
  assert.ok(res.result);
});

// -- templated tool description (a2a-design-5.md §P1-H) ---------------------

test("mcp: tools/list without listPeers keeps the static description", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 1, method: "tools/list" },
    deps: baseDeps(),
  });
  const desc = res.result.tools[0].description;
  assert.ok(
    !desc.includes("Available peers"),
    "static path must not leak a peer header",
  );
  assert.match(desc, /registered in the A2A registry/);
});

test("mcp: tools/list with listPeers injects a summary and excludes self", async () => {
  const peers = [
    { name: "writer", role: "author", skills: ["prose"] }, // self — must be filtered
    { name: "analyst", role: "analyst", skills: ["market data", "charts"] },
    { name: "editor", role: "editor", skills: ["copyedit"] },
  ];
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 2, method: "tools/list" },
    deps: baseDeps({ listPeers: async () => peers }),
  });
  const desc = res.result.tools[0].description;
  assert.match(desc, /Available peers:/);
  assert.match(desc, /- analyst \(market data, charts\)/);
  assert.match(desc, /- editor \(copyedit\)/);
  assert.ok(
    !desc.includes("- writer"),
    "self name must not appear in the tool description",
  );
});

test("mcp: tools/list truncates long peer list with 'and N more'", async () => {
  const peers = Array.from({ length: 20 }, (_, i) => ({
    name: `peer-${i}`,
    skills: [`s${i}`],
  }));
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 3, method: "tools/list" },
    deps: baseDeps({ listPeers: async () => peers }),
  });
  const desc = res.result.tools[0].description;
  assert.match(desc, /- peer-0 /);
  assert.match(desc, /- peer-15 /);
  assert.match(desc, /\.\.\.and 4 more/);
  assert.ok(
    !desc.includes("- peer-16 "),
    "beyond the cap, peers must be elided",
  );
});

test("mcp: tools/list survives listPeers throwing (registry hiccup fallback)", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 4, method: "tools/list" },
    deps: baseDeps({
      listPeers: async () => {
        throw new Error("registry unreachable");
      },
    }),
  });
  assert.ok(res.result, "tools/list must never fail on registry errors");
  const desc = res.result.tools[0].description;
  assert.ok(!desc.includes("Available peers"));
});

test("mcp: tools/list with skills>3 elides the rest in-line", async () => {
  const peers = [
    { name: "polymath", skills: ["a", "b", "c", "d", "e"] },
  ];
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 5, method: "tools/list" },
    deps: baseDeps({ listPeers: async () => peers }),
  });
  const desc = res.result.tools[0].description;
  assert.match(desc, /- polymath \(a, b, c, \.\.\.\)/);
});

// -- P1-M: optional role in peer summary (a2a-design-6.md) ----------------

test("mcp: tools/list omits role by default even when peer.role is set", async () => {
  const peers = [{ name: "analyst", role: "senior market analyst", skills: ["market data"] }];
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 20, method: "tools/list" },
    deps: baseDeps({ listPeers: async () => peers }),
  });
  const desc = res.result.tools[0].description;
  assert.match(desc, /- analyst \(market data\)/);
  assert.ok(
    !desc.includes("[senior market analyst]"),
    "role must not render without the includeRole flag",
  );
});

test("mcp: tools/list renders role in [brackets] when includeRole=true", async () => {
  const peers = [{ name: "analyst", role: "senior market analyst", skills: ["market data"] }];
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 21, method: "tools/list" },
    deps: baseDeps({ listPeers: async () => peers, includeRole: true }),
  });
  const desc = res.result.tools[0].description;
  assert.match(desc, /- analyst \[senior market analyst\] \(market data\)/);
});

test("mcp: tools/list with includeRole=true but empty role omits brackets cleanly", async () => {
  const peers = [{ name: "analyst", role: "", skills: ["market data"] }];
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 22, method: "tools/list" },
    deps: baseDeps({ listPeers: async () => peers, includeRole: true }),
  });
  const desc = res.result.tools[0].description;
  assert.match(desc, /- analyst \(market data\)/);
  assert.ok(!desc.includes("[]"), "empty role must not produce a bare [] artifact");
});

test("mcp: tools/list with only self registered renders static (no summary)", async () => {
  const res = await mcp.handleMcpRequest({
    body: { jsonrpc: "2.0", id: 6, method: "tools/list" },
    deps: baseDeps({
      listPeers: async () => [{ name: "writer", skills: [] }],
    }),
  });
  const desc = res.result.tools[0].description;
  assert.ok(!desc.includes("Available peers"));
});

// -- P2-K: request_id on MCP error data --------------------------------------

test("mcp: tool-call errors carry requestId in data (registry lookup fail)", async () => {
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 7,
      method: "tools/call",
      params: {
        name: "a2a_call_peer",
        arguments: { to: "ghost", message: "hi" },
      },
    },
    deps: baseDeps({
      requestId: "rid-7",
      lookupPeer: async () => {
        const e = new Error("peer 'ghost' not found");
        e.httpStatus = 404;
        throw e;
      },
    }),
  });
  assert.equal(res.error.code, mcp.ERR_A2A_UPSTREAM);
  assert.equal(res.error.data.httpStatus, 404);
  assert.equal(res.error.data.requestId, "rid-7");
});

test("mcp: invalid-params errors also carry requestId", async () => {
  const res = await mcp.handleMcpRequest({
    body: {
      jsonrpc: "2.0",
      id: 8,
      method: "tools/call",
      params: { name: "a2a_call_peer", arguments: { message: "hi" } },
    },
    deps: baseDeps({ requestId: "rid-8" }),
  });
  assert.equal(res.error.code, mcp.ERR_INVALID_PARAMS);
  assert.equal(res.error.data.requestId, "rid-8");
});
