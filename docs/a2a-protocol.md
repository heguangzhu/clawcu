# A2A Protocol Guide

🌐 Language:
[English](a2a-protocol.md) | [中文](a2a-protocol.zh-CN.md)

> This guide covers ClawCU's A2A adapter: what it is, how it works with the Google A2A protocol, and how to operate it. For command-line reference, see [USAGE_latest.md](../release/USAGE_latest.md). For version history, see [CHANGELOG.md](../CHANGELOG.md).

* * *
## TL;DR

- `clawcu create openclaw|hermes --a2a ...` launches a **companion container** running the A2A adapter alongside the service.
- The adapter speaks the standard **Google A2A protocol** (JSON-RPC 2.0) and exposes `GET /.well-known/agent-card.json` (discovery), `POST /` (JSON-RPC messaging), and `POST /mcp` (`a2a_call_peer`).
- Stock instances (no `--a2a`) are unchanged. A2A is strictly opt-in and additive.
- `clawcu a2a registry serve` runs the aggregate registry so instances can discover each other.
- `clawcu a2a send --to <name> --message "..."` is the smoke test.

* * *
## Table of Contents

- [What the adapter is](#what-the-adapter-is)
- [Architecture](#architecture)
- [Opt-in: enabling A2A on an instance](#opt-in-enabling-a2a-on-an-instance)
- [Protocol surface](#protocol-surface)
- [LLM-facing MCP tool](#llm-facing-mcp-tool)
- [The A2A registry](#the-a2a-registry)
- [Two-instance walkthrough](#two-instance-walkthrough)
- [Enabling A2A on an existing instance](#enabling-a2a-on-an-existing-instance)
- [Troubleshooting](#troubleshooting)
- [Current limits](#current-limits)
- [FAQ](#faq)

* * *
## What the adapter is

The **A2A adapter** is a lightweight companion container that runs alongside a managed service container, sharing its Docker network namespace. It translates the standard Google A2A protocol (JSON-RPC 2.0) into the service's native `/v1/chat/completions` API.

The adapter:

1. Publishes a standard **AgentCard** at `GET /.well-known/agent-card.json` so peers can discover this agent.
2. Accepts A2A messages via **JSON-RPC 2.0** at `POST /`, forwarding them to the co-located service gateway.
3. Reports gateway readiness via a health check.

What the adapter is **not**:

- It is not baked into the service image. The service runs its original, unmodified image.
- It is not a reverse proxy. It only speaks A2A protocol on its own port.
- It is not a plugin loaded into the service. The service is completely unaware of A2A.

* * *
## Architecture

```
┌────────────── Docker network namespace ──────────────────────┐
│                                                              │
│   ┌────────────────────┐       ┌────────────────────────┐   │
│   │ Service container  │       │ A2A adapter container  │   │
│   │  (OpenClaw /       │◀─────│  python:3.12-slim      │   │
│   │   Hermes)          │  LLM  │  a2a-sdk + httpx       │   │
│   │  port 18789/8642   │  call │  port 18790 / 9119     │   │
│   └────────────────────┘       └────────────────────────┘   │
│          ▲                              ▲                    │
│          │                              │                    │
│   (existing users)               A2A peers                  │
│                                  (JSON-RPC 2.0)             │
└──────────────────────────────────────────────────────────────┘
                   │ 18819 (service)  │ 18820 (A2A)
                   ▼                  ▼
                host network (127.0.0.1 by default)
```

The adapter container uses `--network container:<service>` to share the service's network stack. This means the adapter can reach the service at `127.0.0.1:<service_port>` with zero extra network hops, while also inheriting the service's published ports.

**Per-service defaults**:

| Service | Gateway port (internal) | Adapter port (internal) | Readiness path |
|---|---|---|---|
| OpenClaw | 18789 | 18790 | `/healthz` |
| Hermes | 8642 | 9119 | `/health` |

* * *
## Opt-in: enabling A2A on an instance

At `clawcu create` time, `--a2a` enables the companion container:

```bash
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a
```

What happens:

1. ClawCU builds a single generic adapter image `clawcu/a2a-adapter:<version>` (if not already present).
2. The service starts from its **original image** — no modifications, no baking.
3. A companion container starts sharing the service's network namespace.
4. `.clawcu-instance.json` records `a2a_enabled: true`.

The adapter image is shared across all A2A instances (OpenClaw and Hermes alike). It's built once and reused.

To verify:

```bash
curl -s http://127.0.0.1:<adapter_port>/.well-known/agent-card.json | jq .
# Standard Google A2A AgentCard with supported_interfaces, capabilities, skills
```

* * *
## Protocol surface

The adapter implements the **Google A2A protocol v0.1** using `a2a-sdk`.

### `GET /.well-known/agent-card.json`

Returns a standard AgentCard:

```json
{
  "name": "writer",
  "description": "writer agent",
  "supported_interfaces": [{"url": "http://127.0.0.1:18820/", "protocol_version": "0.1"}],
  "version": "0.1.0",
  "capabilities": {"streaming": true},
  "skills": [
    {
      "id": "a2a-chat",
      "name": "chat",
      "description": "Send a message to writer",
      "tags": ["chat"]
    }
  ]
}
```

### `POST /` (JSON-RPC 2.0)

The adapter accepts standard A2A JSON-RPC methods:

#### `message/send`

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"type": "text", "text": "summarize yesterday's standup"}]
    }
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "role": "agent",
    "parts": [{"type": "text", "text": "Yesterday's standup focused on..."}]
  }
}
```

#### `tasks/get`, `tasks/cancel`

Standard A2A task lifecycle methods are supported via `a2a-sdk`'s `InMemoryTaskStore`.

### Health check

The adapter probes the service gateway's readiness path (`/healthz` for OpenClaw, `/health` for Hermes) before forwarding messages. If the gateway isn't ready, the task fails with a clear message.

* * *
## LLM-facing MCP tool

The adapter also serves MCP over JSON-RPC at `POST /mcp`, exposing one tool:

- `a2a_call_peer(to, message, registry_url?, timeout_seconds?)`

The tool looks up `to` in the A2A registry, sends a standard A2A `message/send` request to that peer, and returns both text content and structured task data. When an instance is created with `--a2a`, ClawCU writes `mcp.servers.a2a = {"url": "http://127.0.0.1:<adapter_port>/mcp"}` into the service config so the local agent can call peers during a conversation.

* * *
## The A2A registry

The registry aggregates AgentCards from all running managed instances and exposes them at `GET /agents` and `GET /agents/{name}`. Start it with:

```bash
clawcu a2a registry serve
```

It binds `127.0.0.1:9100` by default and runs in the foreground (Ctrl+C to stop). Every managed instance with `--a2a` publishes its card; the registry collects them so peers can find each other.

* * *
## Two-instance walkthrough

The canonical smoke test: two A2A instances talking via the registry.

```bash
# 1. Create two A2A-enabled instances.
clawcu create openclaw --name writer  --version 2026.4.12 --a2a
clawcu create hermes   --name analyst --version 2026.4.13 --a2a

# 2. Start the A2A registry (foreground).
clawcu a2a registry serve

# 3. From another terminal: send a message.
clawcu a2a send --to analyst --message "summarize yesterday"
```

* * *
## Enabling A2A on an existing instance

There is no in-place upgrade from a stock instance to an A2A instance. Use clone-first:

```bash
clawcu clone writer --name writer-a2a
clawcu remove writer-a2a                     # remove the clone's container
clawcu create openclaw --name writer-a2a \
       --version 2026.4.12 --a2a             # recreate from the cloned datadir
```

The datadir (models, history, env) is preserved. The service image stays stock; only the companion container is added.

* * *
## Troubleshooting

**`clawcu a2a send` returns "gateway not ready".**
The adapter started before the service gateway. Wait 10-30 seconds and retry. If it persists, `clawcu logs <instance>` to see why the gateway is stuck.

**`clawcu a2a send` returns an error from the service.**
The adapter forwards whatever the service gateway returns. Check `clawcu logs <instance>` for the underlying provider error (auth, model, quota).

**`curl :<port>/.well-known/agent-card.json` works, but messages hang.**
Usually a model-provider timeout. Use `--timeout 120` on `clawcu a2a send` for longer LLM calls.

**Companion container not running.**
Check with `docker ps | grep clawcu-a2a-<name>`. If missing, `clawcu restart <instance>` will restart both the service and its companion.

**Port conflict on create.**
`clawcu create --a2a` probes ports at create time. Pick a different `--port` or free the port on the host.

* * *
## Current limits

- **No built-in auth.** The A2A endpoint accepts any request from any peer that can reach the port. The adapter binds 127.0.0.1 by default. For multi-host, place it behind a reverse proxy.
- **Local-only registry.** The registry aggregates cards for this host's managed instances. Cross-host federation isn't currently supported.
- **Stock / A2A is a hard switch at create time.** No in-place enable; use clone-first.
- **Companion lifecycle is tied to the service.** When the service stops, the companion should be stopped too (handled automatically by `clawcu start/stop/restart`).

* * *
## FAQ

**Do stock instances cost anything when I'm not using `--a2a`?**
No. The companion container only exists for A2A-enabled instances. Stock instances are unchanged.

**What's the overhead?**
One lightweight Python container using `a2a-sdk`. Memory: <50 MB idle. CPU: zero at rest; per-request cost is dominated by the downstream LLM call.

**Does A2A work with `clawcu exec` / `clawcu tui`?**
Yes. The adapter runs alongside, so `exec` / `tui` / `token` / `config` behave exactly as on a non-A2A instance.

**How does the adapter differ from the old sidecar?**
The old sidecar (v0.3.x) was baked into the service Docker image at create time — modifying the image, injecting an entrypoint supervisor. The new adapter is a separate container sharing the service's network, using the standard Google A2A protocol (JSON-RPC 2.0). This is simpler, more maintainable, and interoperable with third-party A2A clients.

**How do I upgrade the adapter independently of the service?**
`pip install --upgrade clawcu`, then `clawcu recreate <instance>`. The adapter image is rebuilt; the service image stays the same.

* * *

See also:

- [USAGE_latest.md](../release/USAGE_latest.md) — `clawcu a2a` command reference
- [CHANGELOG.md](../CHANGELOG.md) — full version history
