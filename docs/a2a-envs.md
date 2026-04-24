# A2A Environment Variables

Canonical reference for every `A2A_*` / `HERMES_*` / `CLAWCU_A2A_*` env variable read anywhere in `src/clawcu/a2a/`. The three old sources (two sidecars' config/ctx constructors + Adapter injection + control-plane) are consolidated here so operators can grep one place. Review-2 §15.

Scope legend: **CP** = control plane (`clawcu a2a` CLI, builder, registry). **HS** = Hermes sidecar. **OS** = OpenClaw sidecar. **Both** = identical semantics on both sidecars. **Adapter** = set by `a2a/adapter.py` into the container environment.

---

## Identity & endpoint

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_SELF_NAME` | _required_ | Both | Agent name as registered in the registry; also the `from` field of outbound messages. |
| `A2A_SELF_ROLE` | "" | Both | Optional free-form role string surfaced in the tool description when `A2A_TOOL_DESC_INCLUDE_ROLE=true`. |
| `A2A_SELF_SKILLS` | "" | Both | Comma-separated skill tags; surfaced in peer tool-description lines. |
| `A2A_SELF_ENDPOINT` | `http://127.0.0.1:9129/a2a/send` (HS), `http://$ADVERTISE_HOST:$ADVERTISE_PORT/a2a/send` (OS) | Both | Absolute URL peers use to reach this sidecar's `/a2a/send`; written into the agent card. |
| `A2A_SIDECAR_NAME` | — | Adapter | OpenClaw CLI-set name; entrypoint forwards to `A2A_SELF_NAME`. |
| `A2A_SIDECAR_ROLE` | — | Adapter | Ditto for role. |
| `A2A_SIDECAR_SKILLS` | — | Adapter | Ditto for skills. |
| `A2A_SIDECAR_ADVERTISE_HOST` | `127.0.0.1` | OS | Host written into the agent-card endpoint. |
| `A2A_SIDECAR_ADVERTISE_PORT` | `$A2A_PORT` | OS | Port written into the agent-card endpoint. |
| `A2A_SIDECAR_PORT` | 0 (pick free) | Adapter | Host-side published port for the openclaw sidecar container. |
| `A2A_ADVERTISE_HOST` | `127.0.0.1` | HS | Host written into the agent-card endpoint. |
| `A2A_ADVERTISE_PORT` | `9129` | HS | Port written into the agent-card endpoint. |
| `A2A_BIND_HOST` | `0.0.0.0` | HS | Bind address for the `HTTPServer`. |
| `A2A_BIND_PORT` | `9129` | HS | Bind port for the `HTTPServer`. |
| `A2A_PORT` | `9129` | OS | Bind port (single-var; OS also binds `0.0.0.0`). |
| `A2A_ROLE` / `A2A_SKILLS` | — | Adapter | Deprecated aliases of `A2A_SELF_ROLE` / `A2A_SELF_SKILLS`; entrypoint forwards. |
| `A2A_NAME` | — | Adapter | Deprecated alias of `A2A_SELF_NAME`. |

## Upstream (LLM / gateway)

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_MODEL` | — | OS | Model slug passed to OpenClaw host (`clawcu chat`) for each inbound `/a2a/send`. |
| `A2A_UPSTREAM` | — | HS | URL of the Hermes co-resident LLM gateway (used by `/a2a/send` handler to fetch a reply). |
| `A2A_GATEWAY_HOST` | `127.0.0.1` | OS | Hostname of the co-resident OpenClaw host agent. |
| `A2A_GATEWAY_PORT` | `8137` | OS | Port of the co-resident host. |
| `A2A_GATEWAY_READY_PATH` | `/api/session/ensure` | OS | Readiness probe path on the host. |
| `A2A_GATEWAY_READY_DEADLINE_S` | `120` | OS | Seconds to wait for the host to come up before 503-ing. Preferred over the `_MS` form. |
| `A2A_GATEWAY_READY_DEADLINE_MS` | — | OS | Legacy ms form of the above. If both set, `_SECONDS` wins. |
| `A2A_GATEWAY_READY_POLL_S` / `A2A_GATEWAY_READY_PROBE_S` | 0.5 / 2.0 | HS | Poll interval / per-probe timeout for Hermes gateway readiness. |
| `A2A_LOCAL_UPSTREAM_CAP` | 64 MiB | HS | Body cap on the co-resident Hermes gateway response. |

## Timeouts (all seconds unless noted)

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_REQUEST_TIMEOUT_SECONDS` | `60.0` (OS), none (HS: uses `A2A_TIMEOUT_SECONDS`) | OS | Per-`/a2a/send` outbound timeout on openclaw. Preferred over the `_MS` form. |
| `A2A_REQUEST_TIMEOUT_MS` | — | OS | Legacy ms form. If both set, `_SECONDS` wins. |
| `A2A_TIMEOUT_SECONDS` | `60.0` | HS | Per-outbound timeout on hermes (registry + peer). |
| `A2A_INBOUND_REQUEST_TIMEOUT_S` | `60.0` | Both | Server-side socket read timeout per inbound request. |
| `A2A_HOST_ADAPTER_TTL_S` | `60.0` | OS | TTL for the host-adapter docker-exec cache (`read_file`, `get_env`). |

## Rate limit / body cap

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_RATE_LIMIT_PER_MINUTE` | 30 | Both | Inbound `/a2a/send` per-origin rate limit. `0` disables. |
| `A2A_OUTBOUND_RPM` / `A2A_OUTBOUND_RATE_LIMIT` | 60 | Both | Self-origin outbound calls per minute (shared bucket for `/a2a/outbound` + `a2a_call_peer`). |
| `A2A_OUTBOUND_SWEEP_INTERVAL_MS` | 60000 | Both | Sweep timer cadence for the outbound limiter's expired-key GC. |
| `A2A_MAX_BODY_BYTES` | 1 MiB | Both | Inbound request body cap. |
| `A2A_MAX_RESPONSE_BYTES` | 4 MiB | Both | Outbound peer/registry response cap. |
| `A2A_HOP_BUDGET` | 3 | Both | Max `X-A2A-Hop` value accepted; beyond this → 508. |

## Registry

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_REGISTRY_URL` | `http://127.0.0.1:9131` | All | Registry base URL. CP falls back if unset; sidecars require it. |
| `A2A_REGISTRY_TOKEN` | "" | All | Optional bearer token. When set, the registry requires `Authorization: Bearer <token>` on reads, and clients send it automatically. |
| `A2A_ALLOW_CLIENT_REGISTRY_URL` | `false` | Both | Whether `/a2a/outbound` accepts a client-supplied `registry_url` in the body. `false` = strict, safer default. |

## MCP tool description

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_TOOL_DESC_MODE` | dynamic | Both | `static` opts out of peer-list injection into the `a2a_call_peer` description (useful when the registry is flaky). |
| `A2A_TOOL_DESC_INCLUDE_ROLE` | `false` | Both | `true` adds `[role]` to each peer line in the dynamic description. |

## Threading & persistence

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_THREAD_DIR` | tmpdir | Both | Directory where per-thread history files live. |
| `A2A_THREAD_MAX_HISTORY_PAIRS` | 8 | Both | Max user/assistant message pairs retained per thread. |

## Logging

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_LOG_LEVEL` | `INFO` | HS | Stdlib logging level for the hermes sidecar logger. |
| `A2A_SIDECAR_LOG_DIR` | — | Both | Opt-in directory to tee sidecar logs to `<dir>/a2a-sidecar.log`. Best-effort; failures fall back to stderr. |

## Service MCP config (openclaw host only)

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_SERVICE_MCP_CONFIG_PATH` | — | OS adapter | Explicit path to the host's `openclaw.json` (skips container-default lookup). |
| `A2A_SERVICE_MCP_CONFIG_FORMAT` | inferred | OS adapter | `config-json` vs `claude-mcp` format hint for the adapter's card loader. |

## Build plumbing (CI / image build only)

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `A2A_REPO` | official repo | Build | Git repo URL baked into the sidecar image. |
| `A2A_BUILD_ARGS` | "" | Build | Extra args forwarded to `docker build`. |
| `A2A_ENABLED` | — | CP | Feature flag; `false` disables A2A wiring at provider-apply time. |

## Control-plane host identity

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `CLAWCU_A2A_HOST_HOSTNAME` | derived | CP | Hostname used when constructing a host-mode advertise endpoint. |

---

Deprecated / legacy aliases kept for one release; prefer the `_SECONDS` names:
- `A2A_REQUEST_TIMEOUT_MS` → `A2A_REQUEST_TIMEOUT_SECONDS`
- `A2A_GATEWAY_READY_DEADLINE_MS` → `A2A_GATEWAY_READY_DEADLINE_S`
- `A2A_NAME` / `A2A_ROLE` / `A2A_SKILLS` → `A2A_SELF_*`

When two forms of the same knob are set, the `_SECONDS` form wins.
