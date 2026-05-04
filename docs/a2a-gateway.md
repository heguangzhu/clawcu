# A2A Gateway Plan for 0.5.x

> Status: 0.5.x design note.
>
> Snapshot date: 2026-04-29. External project status can change; re-check before choosing a dependency.

## Question

Can an existing open source A2A gateway replace ClawCU's current per-instance companion architecture?

Short answer: there are open source A2A gateway projects, but they are not the same shape as the small local multi-instance router ClawCU needs. None looks like a direct drop-in replacement for the current companion model.

## Existing Gateway Landscape

| Project | Type | Fit for ClawCU local routing |
| --- | --- | --- |
| [`agentgateway`](https://github.com/agentgateway/agentgateway) | Production-oriented AI-native proxy under the Linux Foundation; supports MCP and A2A, with security, observability, governance, routing, capability discovery, and task collaboration. | Closest to a serious gateway, but much heavier than ClawCU's current local companion/router need. Better suited once ClawCU needs platform-grade policy, RBAC, observability, or Kubernetes Gateway API integration. |
| [`Tangle-Two/a2a-gateway`](https://github.com/Tangle-Two/a2a-gateway) | Lightweight Python A2A gateway/registry experiment for publishing agent cards, discovering agents, and sending tasks. | Shape is closer to a registry, but maturity is unclear; no release signal observed in the GitHub page at the time of this note. Treat as a reference, not a dependency. |
| [`inference-gateway/inference-gateway/a2a`](https://pkg.go.dev/github.com/inference-gateway/inference-gateway/a2a) | Go package for configuring multiple A2A agent URLs, including Kubernetes service discovery. | Useful in an inference-gateway or Kubernetes deployment, but not a general local companion replacement. |
| [`opspawn/a2a-x402-gateway`](https://github.com/opspawn/a2a-x402-gateway) | A2A plus x402 micropayment gateway for agent commerce. | Domain-specific to payment/commercial flows, not a general local router. |

## Decision

For 0.5.x, do not replace ClawCU's companion architecture with an external gateway dependency.

Instead, implement a very thin local router inside ClawCU first. The router should solve ClawCU's immediate instance-model problem without importing the complexity of a production gateway stack.

## Proposed Local Router

Working name:

```text
clawcu-a2a-router
```

HTTP surface:

```text
GET  /agents
GET  /agents/{name}
GET  /.well-known/agent-card.json?agent={name}
POST /agents/{name}/
```

The router maintains a small in-memory or file-backed registry:

```text
agent name -> gateway url
agent name -> gateway token
agent name -> role / skills / AgentCard
```

Expected behavior:

- `GET /agents` lists registered ClawCU-managed agents.
- `GET /agents/{name}` returns that agent's routing metadata and card summary.
- `GET /.well-known/agent-card.json?agent={name}` returns the selected agent's AgentCard.
- `POST /agents/{name}/` forwards A2A traffic to the registered gateway URL, attaching the configured gateway token when present.

This keeps the router aligned with ClawCU's local multi-instance model:

```text
local A2A client
      |
      v
clawcu-a2a-router
      |
      +-- writer  -> http://127.0.0.1:<writer-a2a-port>/
      +-- analyst -> http://127.0.0.1:<analyst-a2a-port>/
      +-- editor  -> http://127.0.0.1:<editor-a2a-port>/
```

## Deferred Until Needed

Move toward `agentgateway` or a similar full gateway only when ClawCU actually needs:

- Auth policy beyond local tokens.
- RBAC or tenant isolation.
- Centralized observability and governance.
- Multi-host or Kubernetes-native routing.
- Gateway API integration.
- Production platform controls that outweigh the operational cost.

## Implementation Bias

Keep the first 0.5.x version intentionally boring:

- Prefer one local process over per-feature services.
- Preserve current per-instance companion behavior.
- Use the router as an aggregation and forwarding layer, not as a new agent runtime.
- Keep AgentCard data explicit and inspectable.
- Avoid committing to a third-party gateway abstraction until ClawCU has a real deployment shape that needs it.
